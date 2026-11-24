package com.ufomount.foco.core;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Busca servidores asicam: "ASICAM?" por UDP al puerto 5300 (igual que el PC). */
public final class Discovery {
    public static final int PORT = 5300;
    private static final Pattern NET = Pattern.compile(
            "^(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})(?:\\.(?:\\d{1,3}|x|\\*))?(?:/24)?$");

    public static final class Server {
        public String ip;
        public String host;
        public String camera;
        public String state;
        public final List<String> ips = new ArrayList<>();

        @Override
        public String toString() {
            return host + " (" + ip + ")" + (camera != null ? " · " + camera : " · sin cámara");
        }
    }

    private Discovery() {
    }

    /** Cada IP o red ("192.168.201.46", "192.168.201.0", ".x", "/24") se recorre en su red
     *  /24 completa: el broadcast no cruza routers. Un nombre de equipo va tal cual. */
    public static List<String> expandTargets(Collection<String> targets) {
        List<String> out = new ArrayList<>();
        LinkedHashSet<String> nets = new LinkedHashSet<>();
        if (targets == null) return out;
        for (String t : targets) {
            if (t == null) continue;
            String host = t.trim().split(":")[0];
            if (host.isEmpty()) continue;
            Matcher m = NET.matcher(host);
            if (!m.matches()) {
                out.add(host);
                continue;
            }
            int a = Integer.parseInt(m.group(1));
            int b = Integer.parseInt(m.group(2));
            int c = Integer.parseInt(m.group(3));
            if (a > 255 || b > 255 || c > 255) continue;
            String net = a + "." + b + "." + c;
            if (nets.add(net)) {
                for (int i = 1; i < 255; i++) out.add(net + "." + i);
            }
        }
        return out;
    }

    /** Redes locales con broadcast (WiFi, Ethernet): {ip, broadcast}. */
    public static List<String[]> localNets() {
        List<String[]> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    InetAddress bc = ia.getBroadcast();
                    if (a instanceof Inet4Address && bc != null) {
                        out.add(new String[]{a.getHostAddress(), bc.getHostAddress()});
                    }
                }
            }
        } catch (IOException ignored) {
            // sin permisos o sin red: queda el broadcast general
        }
        return out;
    }

    /** Busca: broadcast local, la red /24 de este equipo (si sweepLocal) y las de `targets`. */
    @SuppressWarnings("unchecked")
    public static List<Server> find(Collection<String> targets, int timeoutMs, boolean sweepLocal) {
        Map<String, Server> found = new LinkedHashMap<>();
        LinkedHashSet<String> dests = new LinkedHashSet<>();
        List<String> sweep = new ArrayList<>();
        if (targets != null) sweep.addAll(targets);
        dests.add("255.255.255.255");
        for (String[] n : localNets()) {
            dests.add(n[1]);
            if (sweepLocal) sweep.add(n[0]);
        }
        dests.addAll(expandTargets(sweep));
        byte[] q = "ASICAM?".getBytes(StandardCharsets.US_ASCII);
        try (DatagramSocket s = new DatagramSocket()) {
            s.setBroadcast(true);
            s.setSoTimeout(200);
            for (String d : dests) {
                try {
                    s.send(new DatagramPacket(q, q.length, InetAddress.getByName(d), PORT));
                } catch (IOException ignored) {
                    // red inalcanzable o nombre que no resuelve
                }
            }
            byte[] buf = new byte[4096];
            long end = System.currentTimeMillis() + timeoutMs;
            while (System.currentTimeMillis() < end) {
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    s.receive(p);
                } catch (SocketTimeoutException e) {
                    continue;
                } catch (IOException e) {
                    continue;   // p. ej. "puerto cerrado" (ICMP) de otro equipo
                }
                String text = new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8);
                if (!text.startsWith("ASICAM ")) continue;
                Object info;
                try {
                    info = MiniJson.parse(text.substring(7));
                } catch (IllegalArgumentException e) {
                    continue;
                }
                if (!(info instanceof Map)) continue;
                Map<String, Object> m = (Map<String, Object>) info;
                String ip = p.getAddress().getHostAddress();
                String host = MiniJson.getString(m, "host", ip);
                Server sv = found.get(host);
                if (sv == null) {
                    sv = new Server();
                    sv.ip = ip;
                    sv.host = host;
                    sv.camera = MiniJson.getString(m, "camera", null);
                    sv.state = MiniJson.getString(m, "state", null);
                    found.put(host, sv);
                }
                if (!sv.ips.contains(ip)) sv.ips.add(ip);
            }
        } catch (IOException ignored) {
            // no se pudo abrir el socket: sin resultados
        }
        return new ArrayList<>(found.values());
    }
}
