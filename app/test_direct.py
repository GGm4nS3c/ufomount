#!/usr/bin/env python3
"""Pruebas de la conexion directa, sin cambiar nada en Windows ni usar la red real:
clientes DHCP falsos y un Windows falso (FakeOps). python app/test_direct.py"""

import os
import random
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import direct_link as D  # noqa: E402

PI = bytes.fromhex("d83add460726")
PC = bytes.fromhex("3c7c3f112233")
IP = "192.168.77.1"
NET = "192.168.77.0/24"
fails = []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("OK " if ok else "FALLA", name, " - %s" % detail if detail else ""))
    if not ok:
        fails.append(name)


def pkt(kind, mac=PI, xid=7, opts=b"", ciaddr="0.0.0.0", op=1):
    return (D.HDR.pack(op, 1, 6, 0, xid, 0, 0, socket.inet_aton(ciaddr), b"\0" * 4, b"\0" * 4, b"\0" * 4,
                       mac + b"\0" * 10, b"", b"") + D.COOKIE + b"\x35\x01" + bytes([kind]) + opts + b"\xff")


def o(code, data):
    return bytes([code, len(data)]) + data


def ipo(code, ip):
    return o(code, socket.inet_aton(ip))


def kind_of(reply):
    return D.parse_options(reply[0][D.HDR.size + 4:])[53][0]


def yi(reply):
    return socket.inet_ntoa(reply[0][16:20])


class FakeOps:
    """Windows falso: anota lo que se le pide."""

    def __init__(self, adapters, dhcp=None, fail=None):
        self.list, self.dhcp, self.fail, self.calls = adapters, dhcp, fail, []
        self.sock = None

    def adapters(self):
        return self.list

    def probe_dhcp(self, a):
        return self.dhcp

    def add_ip(self, a, ip, mask):
        self.calls.append(("add", a["index"], ip, mask))
        if self.fail == "add":
            raise RuntimeError("no pude poner la IP")

    def open_firewall(self):
        self.calls.append(("fw",))
        if self.fail == "fw":
            raise RuntimeError("no pude abrir el firewall")

    def cleanup(self, a):
        self.calls.append(("cleanup", a["index"]))

    def server_socket(self, ip, ifindex):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(0.2)
        self.sock = s
        return s


def adapter(index=5, up=True, ips=(), gw=False, dhcp=True, phys="802.3", hw=True):
    return {"index": index, "name": "Ethernet", "desc": "", "up": up, "hw": hw, "media": "802.3", "phys": phys,
            "mac": "AA-BB-CC-DD-EE-FF", "dhcp": dhcp, "gateway": gw,
            "ips": list(ips) or [{"ip": "169.254.3.4", "len": 16, "origin": "WellKnown"}]}


def wait(cond, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def start(ops, adapter_index=5):
    box = {}

    def launcher(port, token, ifindex):
        box["t"] = threading.Thread(target=lambda: box.update(rc=D.run_helper(port, token, ifindex, ops)), daemon=True)
        box["t"].start()
        return True
    lk = D.DirectLink(launcher)
    lk.start(adapter_index)
    return lk, box


print("DHCP")
leases = []
s = D.DhcpServer(IP, NET, on_lease=lambda m, i: leases.append((m, i)))
r = s.handle(pkt(D.DISCOVER, xid=0x1234, opts=o(61, b"\x01" + PI)))
check("DISCOVER de la Pi recibe OFFER", r and kind_of(r) == D.OFFER and yi(r) == "192.168.77.10", r and yi(r))
check("mismo xid y MAC", r[0][4:8] == struct.pack("!I", 0x1234) and r[0][28:34] == PI)
check("respuesta por broadcast al puerto 68", r[1] == ("255.255.255.255", 68))
op = D.parse_options(r[0][D.HDR.size + 4:])
check("mascara /24 y sin puerta de enlace ni DNS", op[1] == b"\xff\xff\xff\x00" and 3 not in op and 6 not in op)
check("sin OFFER no hay entrega", leases == [])
r = s.handle(pkt(D.REQUEST, opts=o(61, b"\x01" + PI) + ipo(50, "192.168.77.10") + ipo(54, IP)))
check("REQUEST recibe ACK y avisa", kind_of(r) == D.ACK and leases == [(PI, "192.168.77.10")], str(leases))
r = s.handle(pkt(D.REQUEST, opts=o(61, b"\x01" + PI), ciaddr="192.168.77.10"))
check("renovacion: ACK directo a la Pi", kind_of(r) == D.ACK and r[1] == ("192.168.77.10", 68))
r = s.handle(pkt(D.REQUEST, opts=o(61, b"\x01" + PI) + ipo(50, "192.168.200.159")))
check("pide la IP de la red anterior: NAK", kind_of(r) == D.NAK and yi(r) == "0.0.0.0")
r = s.handle(pkt(D.REQUEST, opts=o(61, b"\x01" + PI) + ipo(50, "192.168.77.10") + ipo(54, "192.168.1.1")))
check("REQUEST a otro servidor: silencio", r is None)
r = s.handle(pkt(D.DISCOVER, opts=o(61, b"\xff\x01\x02")))
check("el segundo cliente DHCP de la Pi recibe otra IP", yi(r) == "192.168.77.11", yi(r))
check("el PC u otro equipo: silencio", s.handle(pkt(D.DISCOVER, mac=PC)) is None and PC in s.ignored)
check("Arduino de respaldo: si", s.handle(pkt(D.DISCOVER, mac=bytes.fromhex("0255464f0001"))) is not None)
check("INFORM y RELEASE: silencio", s.handle(pkt(D.INFORM)) is None and s.handle(pkt(D.RELEASE)) is None)
s.handle(pkt(D.DECLINE, opts=o(61, b"\x01" + PI) + ipo(50, "192.168.77.10")))
r = s.handle(pkt(D.DISCOVER, opts=o(61, b"\x01" + PI)))
check("IP rechazada (DECLINE): entrega otra", yi(r) not in ("192.168.77.10", IP), yi(r))
check("respuesta de servidor (op=2): silencio", s.handle(pkt(D.DISCOVER, op=2)) is None)
s2 = D.DhcpServer(IP, NET)
n = sum(1 for i in range(200) if s2.handle(pkt(D.DISCOVER, opts=o(61, struct.pack("!I", i)))))
check("rango agotado: deja de entregar", n == D.LAST_HOST - D.FIRST_HOST + 1, str(n))
rnd = random.Random(1)
bad = 0
for i in range(20000):
    b = bytearray(pkt(rnd.randrange(0, 10), opts=o(61, b"\x01" + PI) + ipo(50, "192.168.77.10")))
    for _ in range(rnd.randrange(1, 8)):
        b[rnd.randrange(len(b))] = rnd.randrange(256)
    b = bytes(b[:rnd.randrange(len(b) + 1)]) if rnd.random() < 0.5 else bytes(b)
    try:
        D.DhcpServer(IP, NET).handle(b)
    except Exception as e:  # noqa: BLE001
        bad += 1
        print("   ", repr(e))
        if bad > 3:
            break
check("20.000 paquetes dañados: ninguno lo bota", bad == 0)

print("Segunda via (puerto 67 ocupado)")


def ip_pkt(payload, sport=68, dport=67, proto=17, frag=0, opts=b""):
    u = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ihl = 5 + len(opts) // 4
    return (struct.pack("!BBHHHBBH4s4s", 0x40 | ihl, 0, ihl * 4 + len(u), 1, frag, 64, proto, 0, b"\0" * 4, b"\xff" * 4)
            + opts + u)


q = pkt(D.DISCOVER)
check("lee un pedido DHCP", D.dhcp_payload(ip_pkt(q)) == (q, "0.0.0.0"))
check("con opciones IP y relleno Ethernet", D.dhcp_payload(ip_pkt(q, opts=b"\x01" * 4) + b"\0" * 9) == (q, "0.0.0.0"))
check("ignora TCP, otros puertos y fragmentos", D.dhcp_payload(ip_pkt(q, proto=6)) is None
      and D.dhcp_payload(ip_pkt(q, dport=68)) is None and D.dhcp_payload(ip_pkt(q, frag=0x2000)) is None)
bad = 0
for i in range(20000):
    b = bytearray(ip_pkt(q))
    for _ in range(rnd.randrange(1, 8)):
        b[rnd.randrange(60)] = rnd.randrange(256)
    try:
        D.dhcp_payload(bytes(b[:rnd.randrange(len(b) + 1)]))
    except Exception as e:  # noqa: BLE001
        bad += 1
        print("   ", repr(e))
        break
check("20.000 paquetes IP dañados: ninguno lo bota", bad == 0)
d = D.udp_datagram(IP, "255.255.255.255", b"hola!")
ph = socket.inet_aton(IP) + b"\xff" * 4 + struct.pack("!BBH", 0, 17, len(d))
check("suma UDP correcta", D.checksum(ph + d) == 0 and struct.unpack("!HH", d[:4]) == (67, 68))
# el choque real de Windows, en otro puerto y sin administrador
w = D.WindowsOps()
h = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
h.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
h.bind(("0.0.0.0", 0))
real67, D.DHCP_PORT = D.DHCP_PORT, h.getsockname()[1]
t0 = time.time()
check("puerto reservado por otro (10013): se detecta", w._udp_socket("127.0.0.1", 1) is None, "%.1f s" % (time.time() - t0))
h.close()
u = w._udp_socket("127.0.0.1", 1)
check("libre otra vez: el socket normal", u is not None)
u.close()
D.DHCP_PORT = real67
check("dice quién usa el puerto", isinstance(w.port67_owners(), str), w.port67_owners())

print("Puertos")
check("con IP de router: no se usa", D.problem(adapter(ips=[{"ip": "192.168.200.10", "len": 24, "origin": "Dhcp"}])) != "")
check("con puerta de enlace: no se usa", D.problem(adapter(gw=True)) != "")
check("sin cable: no se usa", D.problem(adapter(up=False)) != "")
check("solo 169.254: se usa", D.problem(adapter()) == "")
check("WiFi, Bluetooth y virtuales: no", not D.is_wired(adapter(phys="BlueTooth")) and not D.is_wired(adapter(hw=False)))
net = D.pick_subnet([adapter(ips=[{"ip": "192.168.77.5", "len": 24, "origin": "Manual"}])])
check("red elegida no choca con las del PC", str(net) == "192.168.177.0/24", str(net))

print("Ayudante (Windows falso)")
ops = FakeOps([adapter()])
lk, box = start(ops)
check("queda listo", wait(lambda: lk.state == "ready"), lk.state)
check("puso la IP y abrio el firewall", ops.calls == [("add", 5, IP, "255.255.255.0"), ("fw",)], str(ops.calls))
c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
c.bind(("127.0.0.1", 0))
c.sendto(b"basura", ops.sock.getsockname())
c.sendto(pkt(D.DISCOVER, mac=PC), ops.sock.getsockname())
c.sendto(pkt(D.REQUEST, opts=ipo(50, "192.168.77.10") + ipo(54, IP)), ops.sock.getsockname())
check("la IP entregada llega al programa", wait(lambda: lk.mounts == ["192.168.77.10"]), str(lk.mounts))
check("avisa de equipos ajenos", wait(lambda: any("no le contesto" in x for x in lk.lines)))
lk.stop()
check("Detener limpia", wait(lambda: ("cleanup", 5) in ops.calls) and wait(lambda: lk.state == "off"), lk.state)
box["t"].join(5)
check("el ayudante termina", not box["t"].is_alive() and box.get("rc") == 0)

ops = FakeOps([adapter()])
lk, box = start(ops)
wait(lambda: lk.state == "ready")
lk._conn.shutdown(socket.SHUT_RDWR)          # como si el programa se cerrara de golpe
check("el programa se cae: limpia igual", wait(lambda: ("cleanup", 5) in ops.calls))

ops = FakeOps([adapter()], dhcp="192.168.1.1")
lk, box = start(ops)
check("otro servidor DHCP: no toca nada", wait(lambda: lk.state == "error") and ops.calls == [], str(lk.lines[-1:]))
ops = FakeOps([adapter(ips=[{"ip": "192.168.200.10", "len": 24, "origin": "Dhcp"}])])
lk, box = start(ops)
check("puerto con router: no toca nada", wait(lambda: lk.state == "error") and ops.calls == [])
ops = FakeOps([])
lk, box = start(ops)
check("puerto desaparecido: no toca nada", wait(lambda: lk.state == "error") and ops.calls == [])
for where in ("add", "fw"):
    ops = FakeOps([adapter()], fail=where)
    lk, box = start(ops)
    check("falla al %s: avisa y limpia" % ("poner la IP" if where == "add" else "abrir el firewall"),
          wait(lambda: lk.state == "error") and wait(lambda: ("cleanup", 5) in ops.calls), str(lk.lines[-1:]))
lk = D.DirectLink(lambda p, t, i: False)
lk.start(5)
check("se cancela el permiso de Windows", wait(lambda: lk.state == "error"), str(lk.lines[-1:]))

got = {}


def impostor(port, token, ifindex):
    def work():
        a = socket.create_connection(("127.0.0.1", port))      # otro programa, sin la clave
        a.sendall(b'{"kind":"auth","token":"x"}\n{"kind":"ready","ip":"1.2.3.4"}\n')
        got["closed"] = a.recv(10) == b""
        D.run_helper(port, token, ifindex, FakeOps([adapter()]))
    threading.Thread(target=work, daemon=True).start()
    return True


lk = D.DirectLink(impostor)
lk.start(5)
check("otro programa sin la clave es rechazado", wait(lambda: lk.state == "ready") and lk.pc_ip == IP, lk.pc_ip)
lk.stop()
wait(lambda: lk.state == "off")
check("se puede activar otra vez", (lk.start(5), wait(lambda: lk.state == "ready"))[1])
lk.stop()
wait(lambda: lk.state == "off")

print("\nRESUMEN: %s" % ("%d fallas: %s" % (len(fails), "; ".join(fails)) if fails else "0 fallas"))
sys.exit(1 if fails else 0)
