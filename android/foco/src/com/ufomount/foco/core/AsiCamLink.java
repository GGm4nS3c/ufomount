package com.ufomount.foco.core;

import java.io.BufferedInputStream;
import java.io.BufferedReader;
import java.io.Closeable;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/** Conexion con el servidor asicam de la Raspberry: control (JSON por lineas, puerto 5300)
 *  y flujo de cuadros crudos (puerto 5301). Protocolo en asicam/README.md. */
public final class AsiCamLink implements Closeable {
    public static final int CTRL_PORT = 5300;
    public static final int DATA_PORT = 5301;

    public interface Listener {
        /** En el hilo de datos: debe ser rapido. */
        void onFrame(Frame f);

        /** Se conecto (true) o se corto (false, con el motivo) el flujo de cuadros. */
        void onStream(boolean connected, String detail);
    }

    /** El servidor contesto ok=false (p. ej. un ajuste fuera de rango, o sin camara). */
    public static final class ServerError extends IOException {
        public ServerError(String m) {
            super(m);
        }
    }

    public final String host;
    private final int ctrlPort;
    private final int dataPort;
    private Socket ctrl;
    private BufferedReader in;
    private OutputStream out;
    private int reqId;
    private volatile boolean streaming;
    private volatile Socket dataSock;
    private Thread rx;
    // candado propio del flujo: request() puede tener el del objeto tomado hasta 10 s
    // esperando al servidor, y startStream() se llama desde la pantalla
    private final Object streamLock = new Object();

    public AsiCamLink(String host) {
        this(host, CTRL_PORT, DATA_PORT);
    }

    public AsiCamLink(String host, int ctrlPort, int dataPort) {
        this.host = host;
        this.ctrlPort = ctrlPort;
        this.dataPort = dataPort;
    }

    private void openCtrl() throws IOException {
        closeCtrl();
        Socket s = new Socket();
        s.connect(new InetSocketAddress(host, ctrlPort), 4000);
        s.setSoTimeout(10000);
        s.setTcpNoDelay(true);
        ctrl = s;
        in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
        out = s.getOutputStream();
    }

    private void closeCtrl() {
        try {
            if (ctrl != null) ctrl.close();
        } catch (IOException ignored) {
            // ya estaba cerrado
        }
        ctrl = null;
        in = null;
        out = null;
    }

    /** Un pedido de control; si la conexion se cayo, reconecta y reintenta una vez. */
    @SuppressWarnings("unchecked")
    public synchronized Map<String, Object> request(String cmd, Map<String, Object> params) throws IOException {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("cmd", cmd);
        if (params != null) req.putAll(params);
        req.put("id", ++reqId);
        IOException last = null;
        for (int attempt = 0; attempt < 2; attempt++) {
            try {
                if (ctrl == null) openCtrl();
                out.write((MiniJson.stringify(req) + "\n").getBytes(StandardCharsets.UTF_8));
                out.flush();
                String line = in.readLine();
                if (line == null) throw new EOFException("el servidor cerró la conexión");
                Object r = MiniJson.parse(line);
                if (!(r instanceof Map)) throw new IOException("respuesta inválida del servidor");
                Map<String, Object> m = (Map<String, Object>) r;
                if (!Boolean.TRUE.equals(m.get("ok"))) {
                    throw new ServerError(MiniJson.getString(m, "error", "error del servidor"));
                }
                return m;
            } catch (ServerError e) {
                throw e;
            } catch (IOException e) {
                last = e;
                closeCtrl();
            } catch (IllegalArgumentException e) {
                last = new IOException(e.getMessage());
                closeCtrl();
            }
        }
        throw last;
    }

    public Map<String, Object> hello() throws IOException {
        return request("hello", null);
    }

    public Map<String, Object> info() throws IOException {
        return request("info", null);
    }

    public Map<String, Object> status() throws IOException {
        return request("status", null);
    }

    /** Ajustes actuales del servidor. */
    public Map<String, Object> settings() throws IOException {
        return MiniJson.getMap(request("get", null), "settings");
    }

    /** Cambia ajustes (se aplican en vivo); devuelve todos los ajustes resultantes. */
    public Map<String, Object> set(Map<String, Object> changes) throws IOException {
        Map<String, Object> p = new HashMap<>();
        p.put("settings", changes);
        return MiniJson.getMap(request("set", p), "settings");
    }

    /** Empieza a recibir cuadros en un hilo propio; reconecta solo si se corta. */
    public void startStream(final Listener listener) {
        synchronized (streamLock) {
            if (streaming) return;
            streaming = true;
            rx = new Thread(new Runnable() {
                @Override
                public void run() {
                    rxLoop(listener);
                }
            }, "asicam-datos");
            rx.setDaemon(true);
            rx.start();
        }
    }

    public void stopStream() {
        streaming = false;
        Socket s = dataSock;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // ya cerrado
            }
        }
        Thread t = rx;
        if (t != null && t != Thread.currentThread()) {
            try {
                t.join(3000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        rx = null;
    }

    private void rxLoop(Listener l) {
        byte[] hdr = new byte[Frame.HEADER_SIZE];
        while (streaming) {
            Socket s = new Socket();
            try {
                s.setReceiveBufferSize(4 << 20);
                s.connect(new InetSocketAddress(host, dataPort), 4000);
                s.setSoTimeout(15000);
                dataSock = s;
                l.onStream(true, null);
                DataInputStream din = new DataInputStream(new BufferedInputStream(s.getInputStream(), 1 << 16));
                while (streaming) {
                    din.readFully(hdr);
                    Frame f = Frame.parseHeader(hdr);
                    byte[] px = new byte[f.payloadSize];
                    din.readFully(px);
                    f.data = px;
                    l.onFrame(f);
                }
            } catch (IOException e) {
                if (streaming) l.onStream(false, e.getMessage());
            } finally {
                try {
                    s.close();
                } catch (IOException ignored) {
                    // ya cerrado
                }
                dataSock = null;
            }
            if (streaming) {
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException e) {
                    break;
                }
            }
        }
    }

    @Override
    public void close() {
        stopStream();
        synchronized (this) {
            closeCtrl();
        }
    }
}
