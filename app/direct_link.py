#!/usr/bin/env python3
"""Conexion directa PC - montura, sin router ni servidor DHCP.

La Raspberry pide su IP por DHCP. Sin un router que la entregue no hay como llegar a ella;
aqui el PC hace ese papel mientras dura la conexion:

    1. agrega una IP al puerto Ethernet elegido (p. ej. 192.168.77.1), hasta detener o
       reiniciar el PC: no borra las IPs que ya tenia;
    2. entrega una IP por DHCP a la montura, y SOLO a la montura (se reconoce por el
       fabricante de su MAC: Raspberry Pi, o la shield del Arduino de respaldo);
    3. al detener, o si el programa se cierra, deja el puerto como estaba.

Para no estorbar en una red que ya tiene router, no arranca si el puerto tiene puerta de
enlace o una IP recibida por DHCP, ni si otro servidor DHCP contesta.

Cambiar la IP del PC necesita permisos de administrador. El programa sigue sin ellos: solo
un ayudante (el mismo programa con --direct-helper) se abre con el aviso de Windows (UAC).
Los dos hablan por un socket local con una clave de un solo uso; si el programa se cierra,
el ayudante lo nota, limpia y termina.

No hay que cambiar nada en la Raspberry. Solo Windows.
"""

import ctypes
import ipaddress
import json
import os
import random
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

SUBNETS = ("192.168.77.0/24", "192.168.177.0/24", "10.77.77.0/24", "172.27.77.0/24")
FIRST_HOST, LAST_HOST = 10, 60          # rango que se entrega
LEASE_S = 3600
RULE_NAME = "UFOMOUNT conexion directa (DHCP)"
CTRL_PORT = 5300

# fabricantes de MAC a los que se les entrega IP: nadie mas recibe respuesta
MOUNT_MACS = ("b827eb", "dca632", "e45f01", "d83add", "2ccf67", "28cdc1", "88a29e",   # Raspberry Pi
              "0255464f")                                                             # Arduino de respaldo

DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
COOKIE = b"\x63\x82\x53\x63"
HDR = struct.Struct("!BBBBIHH4s4s4s4s16s64s128s")


# ----------------------------------------------------------------------------
# DHCP
# ----------------------------------------------------------------------------

def parse_options(data):
    out, i = {}, 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        n = data[i + 1]
        if i + 2 + n > len(data):
            break
        out[code] = data[i + 2:i + 2 + n]
        i += 2 + n
    return out


def mac_text(mac):
    return ":".join("%02x" % b for b in mac)


class DhcpServer:
    """Servidor DHCP minimo. handle() no toca la red: recibe un paquete y devuelve
    (respuesta, (ip, puerto)) o None."""

    def __init__(self, server_ip, network, on_lease=None, allowed=MOUNT_MACS):
        self.ip = socket.inet_aton(server_ip)
        self.net = ipaddress.ip_network(network)
        self.allowed = tuple(allowed)
        self.on_lease = on_lease or (lambda mac, ip: None)
        self.leases = {}             # (mac, client-id) -> ip
        self.bad = set()             # IPs que un cliente rechazo (ya usadas)
        self.ignored = set()         # MACs ajenas a las que no se contesto

    def allows(self, mac):
        return mac.hex().startswith(self.allowed)

    def _assign(self, key):
        # la Pi tiene dos clientes DHCP (dhcpcd y NetworkManager): cada uno con su IP, igual
        # que con un router
        if key in self.leases:
            return self.leases[key]
        used = set(self.leases.values()) | self.bad | {self.ip}
        for n in range(FIRST_HOST, LAST_HOST + 1):
            ip = self.net[n].packed
            if ip not in used:
                self.leases[key] = ip
                return ip
        return None

    def handle(self, data):
        if len(data) < HDR.size + 4 or data[HDR.size:HDR.size + 4] != COOKIE:
            return None
        op, htype, hlen, _, xid, _, flags, ciaddr, _, _, giaddr, chaddr, _, _ = HDR.unpack(data[:HDR.size])
        if op != 1 or htype != 1 or hlen != 6:
            return None
        mac = chaddr[:6]
        if not self.allows(mac):
            self.ignored.add(mac)
            return None
        opt = parse_options(data[HDR.size + 4:])
        kind = opt.get(53, b"\0")[:1]
        kind = kind[0] if kind else 0
        key = (mac, bytes(opt.get(61, b"")))
        server_id = opt.get(54)
        if server_id is not None and len(server_id) != 4:
            return None
        if kind == DISCOVER:
            ip = self._assign(key)
            return None if ip is None else self._reply(OFFER, xid, flags, chaddr, ip, giaddr, ciaddr=b"\0" * 4)
        if kind == REQUEST:
            if server_id is not None and server_id != self.ip:
                return None          # le contesta a otro servidor
            asked = opt.get(50, ciaddr)
            ip = self._assign(key)
            if ip is None:
                return None
            if asked != ip:
                # p. ej. la IP que tenia en la red anterior: que empiece de nuevo, ya
                return self._reply(NAK, xid, flags, chaddr, None, giaddr, ciaddr=b"\0" * 4)
            self.on_lease(mac, socket.inet_ntoa(ip))
            return self._reply(ACK, xid, flags, chaddr, ip, giaddr, ciaddr)
        if kind == DECLINE:
            asked = opt.get(50)
            if asked and self.leases.get(key) == asked:
                self.bad.add(asked)
                self.leases.pop(key, None)
        # RELEASE: se conserva la reserva, asi recibe la misma IP al volver
        return None

    def _reply(self, kind, xid, flags, chaddr, ip, giaddr, ciaddr):
        pkt = HDR.pack(2, 1, 6, 0, xid, 0, flags, b"\0" * 4 if kind == NAK else ciaddr, ip or b"\0" * 4,
                       b"\0" * 4, giaddr, chaddr, b"", b"")
        opts = b"\x35\x01" + bytes([kind]) + b"\x36\x04" + self.ip
        if kind != NAK:
            mask = self.net.netmask.packed
            opts += b"\x33\x04" + struct.pack("!I", LEASE_S) + b"\x01\x04" + mask
            opts += b"\x1c\x04" + self.net.broadcast_address.packed
            # sin puerta de enlace ni DNS a proposito: por este cable no hay internet
        pkt += COOKIE + opts + b"\xff"
        pkt += b"\0" * max(0, 300 - len(pkt))
        dst = socket.inet_ntoa(ciaddr) if kind == ACK and ciaddr != b"\0" * 4 else "255.255.255.255"
        return pkt, (dst, 68)


def probe_packet(mac, xid):
    """DHCPDISCOVER para saber si ya hay un servidor en el cable."""
    return (HDR.pack(1, 1, 6, 0, xid, 0, 0x8000, b"\0" * 4, b"\0" * 4, b"\0" * 4, b"\0" * 4,
                     mac + b"\0" * 10, b"", b"") + COOKIE + b"\x35\x01\x01\x37\x02\x01\x03\xff")


# ----------------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------------

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PS_ADAPTERS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
$r = foreach ($a in Get-NetAdapter) {
  $ip = @(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4)
  $if4 = Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4
  $gw = @(Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0')
  [pscustomobject]@{ index=[int]$a.ifIndex; name=$a.Name; desc=$a.InterfaceDescription;
    up=("$($a.Status)" -eq 'Up'); hw=[bool]$a.HardwareInterface; media="$($a.MediaType)"; phys="$($a.PhysicalMediaType)";
    mac="$($a.MacAddress)"; dhcp=("$($if4.Dhcp)" -eq 'Enabled'); gateway=($gw.Count -gt 0);
    ips=@($ip | ForEach-Object { @{ ip="$($_.IPAddress)"; len=[int]$_.PrefixLength; origin="$($_.PrefixOrigin)" } }) }
}
ConvertTo-Json -Compress -Depth 5 @($r)
"""


def system_tool(*parts):
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", *parts)


def run(args, timeout=25):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, creationflags=NO_WINDOW)


def all_adapters():
    p = run([system_tool("WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive",
             "-Command", _PS_ADAPTERS], timeout=60)
    data = json.loads(p.stdout.decode("utf-8", "replace").strip() or "[]")
    return data if isinstance(data, list) else [data]


def is_wired(a):
    """Ethernet de verdad (tambien adaptadores USB); ni WiFi, ni Bluetooth, ni virtuales."""
    return bool(a.get("hw")) and a.get("media") == "802.3" and a.get("phys") not in ("BlueTooth", "Native 802.11")


def problem(a):
    """Por que no se puede usar este puerto ("" si se puede)."""
    if not a.get("up"):
        return "sin cable o con la montura apagada"
    if any(i.get("origin") == "Dhcp" for i in a.get("ips", ())):
        return "ya recibe IP de un router: usa Buscar"
    if a.get("gateway"):
        return "tiene puerta de enlace (hay un router): usa Buscar"
    return ""


def pick_subnet(adapters):
    """La primera red que no choque con las que el PC ya usa."""
    used = []
    for a in adapters:
        for i in a.get("ips", ()):
            try:
                used.append(ipaddress.ip_network("%s/%d" % (i["ip"], i["len"]), strict=False))
            except (ValueError, KeyError, TypeError):
                pass
    for s in SUBNETS:
        net = ipaddress.ip_network(s)
        if not any(net.overlaps(u) for u in used):
            return net
    return None


class WindowsOps:
    """Los cambios en Windows; necesita administrador."""

    def __init__(self):
        self.netsh = system_tool("netsh.exe")
        self.coexist_set = False
        self.ip_added = None
        self.had_dhcp = False

    def adapters(self):
        return all_adapters()

    def _netsh(self, *args):
        p = run([self.netsh] + list(args))
        return p.returncode == 0, p.stdout.decode("oem", "replace").strip()

    def probe_dhcp(self, a, secs=3.0):
        """IP de otro servidor DHCP que conteste en el puerto, o None."""
        src = next((i["ip"] for i in a.get("ips", ())), None)
        if src is None:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((src, 68))
            s.settimeout(0.3)
            xid = random.getrandbits(32)
            mac = bytes.fromhex(a.get("mac", "").replace("-", "").replace(":", "")) or b"\x02\0\0\0\0\1"
            s.sendto(probe_packet(mac, xid), ("255.255.255.255", 67))
            end = time.monotonic() + secs
            while time.monotonic() < end:
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(data) >= HDR.size and data[0] == 2 and struct.unpack("!I", data[4:8])[0] == xid:
                    return addr[0]
        except OSError:
            pass                     # no se pudo preguntar: quedan las otras comprobaciones
        finally:
            s.close()
        return None

    def add_ip(self, a, ip, mask):
        idx = str(int(a["index"]))
        self.had_dhcp = bool(a.get("dhcp"))
        if self.had_dhcp:
            _, show = self._netsh("interface", "ipv4", "show", "interface", idx)
            on = any("coexist" in ln.lower() and "enabled" in ln.lower() for ln in show.splitlines())
            if not on:
                ok, msg = self._netsh("interface", "ipv4", "set", "interface", idx,
                                      "dhcpstaticipcoexistence=enabled", "store=active")
                if not ok:
                    raise RuntimeError("Windows no permitió agregar una IP sin quitar el DHCP del puerto: " + msg)
                self.coexist_set = True
        ok, msg = self._netsh("interface", "ipv4", "add", "address", idx, ip, mask, "store=active")
        if not ok:
            raise RuntimeError("no pude poner la IP %s en el puerto: %s" % (ip, msg))
        self.ip_added = ip

    def open_firewall(self):
        self._netsh("advfirewall", "firewall", "delete", "rule", "name=" + RULE_NAME)
        ok, msg = self._netsh("advfirewall", "firewall", "add", "rule", "name=" + RULE_NAME, "dir=in",
                              "action=allow", "protocol=UDP", "localport=67", "remoteport=68",
                              "program=" + sys.executable, "profile=any")
        if not ok:
            raise RuntimeError("no pude abrir el firewall para DHCP: " + msg)

    def cleanup(self, a):
        self._netsh("advfirewall", "firewall", "delete", "rule", "name=" + RULE_NAME)
        if a is None:
            return
        idx = str(int(a["index"]))
        if self.ip_added:
            self._netsh("interface", "ipv4", "delete", "address", idx, self.ip_added)
        if self.coexist_set:
            self._netsh("interface", "ipv4", "set", "interface", idx,
                        "dhcpstaticipcoexistence=disabled", "store=active")
        if self.had_dhcp:            # por si Windows apago el DHCP del puerto
            try:
                now = next((x for x in self.adapters() if x["index"] == a["index"]), None)
            except (OSError, ValueError, subprocess.SubprocessError):
                now = None
            if now is not None and not now.get("dhcp"):
                self._netsh("interface", "ipv4", "set", "address", idx, "source=dhcp")

    def server_socket(self, ip, ifindex):
        """Socket en el puerto 67 de esa IP: en Windows recibe los broadcast de ese puerto
        Ethernet y de ningun otro."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:                         # IP_UNICAST_IF: que los broadcast salgan por este puerto
            s.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", int(ifindex)))
        except OSError:
            pass
        end = time.monotonic() + 12  # Windows tarda unos segundos en dar por buena la IP nueva
        while True:
            try:
                s.bind((ip, 67))
                s.settimeout(0.5)
                return s
            except OSError as e:
                if time.monotonic() > end:
                    s.close()
                    raise RuntimeError("no pude escuchar DHCP en %s: %s" % (ip, e))
                time.sleep(0.5)


# ----------------------------------------------------------------------------
# Ayudante (con permisos de administrador)
# ----------------------------------------------------------------------------

def run_helper(port, token, ifindex, ops=None):
    """Corre hasta que el programa cierre el socket o mande "stop". Siempre limpia."""
    ops = ops or WindowsOps()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        return 1
    lock = threading.Lock()
    stop = threading.Event()

    def say(kind, **kw):
        try:
            with lock:
                c.sendall((json.dumps(dict(kw, kind=kind)) + "\n").encode("utf-8"))
        except OSError:
            stop.set()

    def watch():
        try:
            c.settimeout(None)
            while True:
                data = c.recv(256)
                if not data or b"stop" in data:
                    break
        except OSError:
            pass
        stop.set()

    adapter = sock = None
    touched = False
    try:
        say("auth", token=token)
        threading.Thread(target=watch, daemon=True).start()
        say("info", text="revisando el puerto Ethernet…")
        adapters = ops.adapters()
        adapter = next((a for a in adapters if a.get("index") == ifindex), None)
        if adapter is None or not is_wired(adapter):
            raise RuntimeError("ese puerto Ethernet ya no está")
        why = problem(adapter)
        if why:
            raise RuntimeError("%s: %s" % (adapter["name"], why))
        other = ops.probe_dhcp(adapter)
        if other:
            raise RuntimeError("ya hay un servidor DHCP en ese cable (%s): usa Buscar" % other)
        net = pick_subnet(adapters)
        if net is None:
            raise RuntimeError("no encontré una red libre para la conexión directa")
        if stop.is_set():
            return 0
        ip = str(net[1])
        touched = True
        ops.add_ip(adapter, ip, str(net.netmask))
        ops.open_firewall()
        sock = ops.server_socket(ip, adapter["index"])
        srv = DhcpServer(ip, str(net), on_lease=lambda m, i: say("lease", mac=mac_text(m), ip=i))
        say("ready", ip=ip, net=str(net))
        told = set()
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            try:
                out = srv.handle(data)
            except (struct.error, ValueError, IndexError):
                continue             # paquete mal formado
            if out is not None:
                try:
                    sock.sendto(*out)
                except OSError as e:
                    say("info", text="no pude contestar por el puerto: %s" % e)
            for mac in srv.ignored - told:
                told.add(mac)
                say("info", text="otro equipo pide IP (%s): no es la montura, no le contesto" % mac_text(mac))
        return 0
    except Exception as e:  # noqa: BLE001 - el motivo se muestra en el programa
        say("error", text=str(e) or e.__class__.__name__)
        return 1
    finally:
        if sock is not None:
            sock.close()
        if touched:
            try:
                ops.cleanup(adapter)
            except Exception as e:  # noqa: BLE001
                say("error", text="no pude dejar el puerto como estaba: %s" % e)
        say("closed")
        try:
            c.close()
        except OSError:
            pass


def launch_elevated(port, token, ifindex):
    """Abre el ayudante con el aviso de Windows (UAC). False si se cancelo."""
    args = ["--direct-helper", "--direct-port", str(int(port)), "--direct-token", token,
            "--direct-if", str(int(ifindex))]
    if not getattr(sys, "frozen", False):
        args.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ufomount.py"))
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.restype = ctypes.c_void_p
    rc = shell(None, "runas", sys.executable, subprocess.list2cmdline(args), None, 0)
    return (rc or 0) > 32


# ----------------------------------------------------------------------------
# Lado del programa (sin permisos)
# ----------------------------------------------------------------------------

class DirectLink:
    """Estado de la conexion directa; sobrevive al cambio de ventana."""

    def __init__(self, launcher=launch_elevated):
        self.launcher = launcher
        self._lock = threading.Lock()
        self._gen = 0
        self._srv = self._conn = None
        self.state = "off"           # off, starting, ready, error
        self.lines = []
        self.pc_ip = ""
        self.mounts = []             # IPs entregadas

    def _line(self, text):
        self.lines.append(text)
        del self.lines[:-40]

    @property
    def active(self):
        return self.state in ("starting", "ready")

    def start(self, ifindex):
        with self._lock:
            if self.active:
                return
            self._gen += 1
            gen = self._gen
            self.state, self.lines, self.pc_ip, self.mounts = "starting", [], "", []
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            self._srv = srv
        threading.Thread(target=self._run, args=(gen, srv, ifindex), daemon=True).start()

    def _fail(self, gen, text):
        if gen == self._gen:
            self._line(text)
            self.state = "error"

    def _run(self, gen, srv, ifindex):
        token = secrets.token_hex(16)
        conn = None
        try:
            self._line("esperando el permiso de Windows…")
            if not self.launcher(srv.getsockname()[1], token, ifindex):
                self._fail(gen, "no diste el permiso de administrador: no se cambió nada")
                return
            end = time.monotonic() + 45
            while conn is None:
                srv.settimeout(max(0.1, end - time.monotonic()))
                cand, _ = srv.accept()
                cand.settimeout(10)
                f = cand.makefile("rb")
                try:
                    msg = json.loads(f.readline(4096).decode("utf-8"))
                    if isinstance(msg, dict) and secrets.compare_digest(str(msg.get("token", "")), token):
                        conn = cand
                        break
                except (OSError, ValueError):
                    pass
                f.close()            # makefile() mantiene vivo el socket
                cand.close()
            conn.settimeout(None)
            with self._lock:
                if gen != self._gen:
                    return
                self._conn = conn
            failed = False
            for raw in f:
                try:
                    m = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                kind = m.get("kind")
                if gen != self._gen:
                    return
                if kind == "ready":
                    self.pc_ip = m.get("ip", "")
                    self._line("este PC quedó en %s; esperando a que la montura pida su IP…" % self.pc_ip)
                    self.state = "ready"
                elif kind == "lease":
                    ip = m.get("ip", "")
                    if ip and ip not in self.mounts:
                        self.mounts.append(ip)
                        self._line("IP entregada a la montura: %s (%s)" % (ip, m.get("mac", "")))
                elif kind == "error":
                    failed = True
                    self._fail(gen, m.get("text", "error"))
                elif kind == "info":
                    self._line(m.get("text", ""))
                elif kind == "closed":
                    break
            if gen == self._gen and not failed:
                self._line("conexión directa detenida: el puerto quedó como estaba")
                self.state = "off"
        except socket.timeout:
            self._fail(gen, "el ayudante no contestó")
        except OSError as e:
            self._fail(gen, "se cortó la conexión con el ayudante: %s" % e)
        finally:
            for s in (conn, srv):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
            with self._lock:
                if gen == self._gen:
                    self._conn = self._srv = None

    def stop(self):
        """Pide al ayudante que limpie. Si el programa se cierra sin esto, lo hace igual."""
        with self._lock:
            conn, srv = self._conn, self._srv
            if conn is None:         # aun esperando el permiso
                self._gen += 1
                if self.state == "starting":
                    self.state = "off"
                    self._line("cancelado")
        for s in ((srv,) if conn is None else ()):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if conn is not None:
            try:
                conn.sendall(b"stop\n")
            except OSError:
                pass

    def reachable(self, timeout=0.6):
        """La primera IP entregada donde ya contesta el servidor de la montura, o None."""
        for ip in list(self.mounts):
            for port in (CTRL_PORT, 5000):
                try:
                    socket.create_connection((ip, port), timeout=timeout).close()
                    return ip
                except OSError:
                    pass
        return None
