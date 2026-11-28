#!/usr/bin/env python3
r"""Compila UFOMOUNT Foco (APK) solo con el JDK y el SDK de Android, sin Gradle.

Uso:
    python android/foco/build.py                      -> android/foco/build/UfomountFoco.apk
    python android/foco/build.py --test 192.168.201.0 -> ademas prueba el nucleo en el PC contra la Pi
    python android/foco/build.py --install            -> ademas la instala en el telefono por USB (adb)

Rutas: JAVA_HOME (por defecto C:\Android\jdk-17) y ANDROID_HOME (por defecto C:\Android\sdk),
con platforms;android-35 y build-tools;35.0.0 instalados.
Firma con una llave propia que se crea la primera vez FUERA del repo (junto al SDK,
ufomount-foco.keystore): siempre la misma, asi la app se puede actualizar sin desinstalar.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_CODE = 1
VERSION_NAME = "0.1.0"
MIN_SDK = 26
TARGET_SDK = 35
BUILD_TOOLS = "35.0.0"
KEY_ALIAS = "foco"
KEY_PASS = "android"     # llave de uso propio (instalacion directa), no de tienda


def run(cmd, env):
    print("> " + os.path.basename(cmd[0]) + " " + " ".join(cmd[1:4]) + (" ..." if len(cmd) > 4 else ""), flush=True)
    subprocess.run(cmd, check=True, env=env)


def main():
    ap = argparse.ArgumentParser(description="Compila UFOMOUNT Foco")
    ap.add_argument("--test", nargs="?", const="", metavar="IP_O_RED",
                    help="compila y corre la prueba del nucleo en el PC (contra la Pi)")
    ap.add_argument("--install", action="store_true", help="instala la APK por USB con adb")
    a = ap.parse_args()

    jdk = os.environ.get("JAVA_HOME") or r"C:\Android\jdk-17"
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or r"C:\Android\sdk"
    exe = ".exe" if os.name == "nt" else ""
    bat = ".bat" if os.name == "nt" else ""
    javac = os.path.join(jdk, "bin", "javac" + exe)
    java = os.path.join(jdk, "bin", "java" + exe)
    keytool = os.path.join(jdk, "bin", "keytool" + exe)
    bt = os.path.join(sdk, "build-tools", BUILD_TOOLS)
    aapt2 = os.path.join(bt, "aapt2" + exe)
    d8 = os.path.join(bt, "d8" + bat)
    zipalign = os.path.join(bt, "zipalign" + exe)
    apksigner = os.path.join(bt, "apksigner" + bat)
    adb = os.path.join(sdk, "platform-tools", "adb" + exe)
    android_jar = os.path.join(sdk, "platforms", "android-%d" % TARGET_SDK, "android.jar")
    for f in (javac, aapt2, d8, zipalign, apksigner, android_jar):
        if not os.path.exists(f):
            sys.exit("falta %s (ver android/README.md: herramientas)" % f)
    env = dict(os.environ, JAVA_HOME=jdk, PATH=os.path.join(jdk, "bin") + os.pathsep + os.environ.get("PATH", ""))

    out = os.path.join(HERE, "build")
    for d in ("gen", "classes", "dex"):
        shutil.rmtree(os.path.join(out, d), ignore_errors=True)
        os.makedirs(os.path.join(out, d))
    unsigned = os.path.join(out, "app-sin-firmar.apk")
    aligned = os.path.join(out, "app-alineada.apk")
    final = os.path.join(out, "UfomountFoco.apk")

    # 1) recursos y manifiesto
    run([aapt2, "compile", "--dir", os.path.join(HERE, "res"), "-o", os.path.join(out, "res.zip")], env)
    run([aapt2, "link", "-o", unsigned, "-I", android_jar,
         "--manifest", os.path.join(HERE, "AndroidManifest.xml"), "--java", os.path.join(out, "gen"),
         "--min-sdk-version", str(MIN_SDK), "--target-sdk-version", str(TARGET_SDK),
         "--version-code", str(VERSION_CODE), "--version-name", VERSION_NAME,
         os.path.join(out, "res.zip")], env)

    # 2) Java (nivel 8: d8 lo pasa a Android)
    sources = glob.glob(os.path.join(HERE, "src", "**", "*.java"), recursive=True)
    sources += glob.glob(os.path.join(out, "gen", "**", "*.java"), recursive=True)
    run([javac, "-encoding", "UTF-8", "-source", "8", "-target", "8", "-Xlint:-options",
         "-bootclasspath", android_jar, "-d", os.path.join(out, "classes")] + sources, env)

    # 3) dex
    jar = os.path.join(out, "classes.jar")
    with zipfile.ZipFile(jar, "w") as z:
        base = os.path.join(out, "classes")
        for f in glob.glob(os.path.join(base, "**", "*.class"), recursive=True):
            z.write(f, os.path.relpath(f, base).replace(os.sep, "/"))
    run([d8, "--release", "--min-api", str(MIN_SDK), "--lib", android_jar,
         "--output", os.path.join(out, "dex"), jar], env)
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(out, "dex", "classes.dex"), "classes.dex")

    # 4) alinear y firmar
    run([zipalign, "-f", "-p", "4", unsigned, aligned], env)
    ks = os.path.join(os.path.dirname(os.path.abspath(sdk)), "ufomount-foco.keystore")
    if not os.path.exists(ks):
        run([keytool, "-genkeypair", "-keystore", ks, "-storepass", KEY_PASS, "-keypass", KEY_PASS,
             "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=UFOMOUNT Foco, O=ufomount"], env)
    run([apksigner, "sign", "--ks", ks, "--ks-pass", "pass:" + KEY_PASS, "--key-pass", "pass:" + KEY_PASS,
         "--ks-key-alias", KEY_ALIAS, "--out", final, aligned], env)
    run([apksigner, "verify", final], env)
    for f in (unsigned, aligned):
        os.remove(f)
    print("\nAPK: %s (%.0f KB)" % (final, os.path.getsize(final) / 1024.0))

    if a.test is not None:
        desk = os.path.join(out, "desktop")
        shutil.rmtree(desk, ignore_errors=True)
        os.makedirs(desk)
        core = glob.glob(os.path.join(HERE, "src", "com", "ufomount", "foco", "core", "*.java"))
        run([javac, "-encoding", "UTF-8", "-source", "8", "-target", "8", "-Xlint:-options", "-d", desk]
            + core + [os.path.join(HERE, "test", "DesktopTest.java")], env)
        subprocess.run([java, "-cp", desk, "DesktopTest"] + ([a.test] if a.test else []), check=True, env=env)

    if a.install:
        if not os.path.exists(adb):
            sys.exit("falta adb (platform-tools)")
        run([adb, "install", "-r", final], env)


if __name__ == "__main__":
    main()
