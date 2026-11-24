import com.ufomount.foco.core.AsiCamLink;
import com.ufomount.foco.core.Discovery;
import com.ufomount.foco.core.Focus;
import com.ufomount.foco.core.Frame;
import com.ufomount.foco.core.MiniJson;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Prueba en el PC del nucleo de la app (el mismo codigo que corre en Android).
 *  Uso: java -cp build/desktop DesktopTest [IP o red, p. ej. 192.168.201.0]
 *  Cambia ajustes de la camara un momento y al final los deja como estaban. */
public class DesktopTest {
    static int total;
    static int fails;

    static void check(String name, boolean ok, String detail) {
        total++;
        if (!ok) fails++;
        System.out.println("  [" + (ok ? "OK " : "FALLA") + "] " + name
                + (detail == null || detail.isEmpty() ? "" : " - " + detail));
    }

    public static void main(String[] args) throws Exception {
        String target = args.length > 0 ? args[0] : "";

        System.out.println("1) JSON y redes");
        String js = "{\"a\":1,\"b\":[true,null,\"x\\\"y\"],\"c\":2.5,\"d\":{\"e\":-3}}";
        check("JSON ida y vuelta", MiniJson.stringify(MiniJson.parse(js)).equals(js), null);
        check("192.168.201.0 recorre 254 direcciones",
                Discovery.expandTargets(Arrays.asList("192.168.201.0")).size() == 254, null);
        check("IP:puerto recorre su red", Discovery.expandTargets(Arrays.asList("192.168.201.46:5000")).size() == 254, null);
        check("nombre de equipo tal cual",
                Discovery.expandTargets(Arrays.asList("astroberry.local")).equals(Arrays.asList("astroberry.local")), null);
        check("IP invalida se ignora", Discovery.expandTargets(Arrays.asList("300.1.1.1")).isEmpty(), null);

        System.out.println("2) Busqueda");
        long t0 = System.currentTimeMillis();
        List<Discovery.Server> found = Discovery.find(
                target.isEmpty() ? Collections.<String>emptyList() : Arrays.asList(target), 1500, true);
        check("encuentra el servidor", !found.isEmpty(),
                found + String.format(" en %.1f s", (System.currentTimeMillis() - t0) / 1000.0));
        if (found.isEmpty()) {
            end();
            return;
        }
        Discovery.Server sv = found.get(0);

        System.out.println("3) Control");
        AsiCamLink link = new AsiCamLink(sv.ip);
        Map<String, Object> h = link.hello();
        check("hello", "asicam".equals(h.get("server")), "version " + h.get("version") + ", equipo " + h.get("host"));
        Map<String, Object> cam = MiniJson.getMap(link.info(), "camera");
        check("info de la camara", cam != null, cam == null ? "" : cam.get("name") + " " + cam.get("max_width") + "x" + cam.get("max_height"));
        if (cam == null) {
            link.close();
            end();
            return;
        }
        int maxW = (int) MiniJson.getLong(cam, "max_width", 1920);
        int maxH = (int) MiniJson.getLong(cam, "max_height", 1080);
        Map<String, Object> original = link.settings();
        try {
            Map<String, Object> bad = new HashMap<>();
            bad.put("gain", -1L);
            link.set(bad);
            check("valor invalido -> error del servidor", false, null);
        } catch (AsiCamLink.ServerError e) {
            check("valor invalido -> error del servidor", true, e.getMessage());
        }

        final List<Frame> frames = Collections.synchronizedList(new ArrayList<Frame>());
        link.startStream(new AsiCamLink.Listener() {
            @Override
            public void onFrame(Frame f) {
                frames.add(f);
                if (frames.size() > 60) frames.remove(0);
            }

            @Override
            public void onStream(boolean connected, String detail) {
                System.out.println("     flujo: " + (connected ? "conectado" : "cortado (" + detail + ")"));
            }
        });
        try {
            System.out.println("4) Modo Completa (bin 2)");
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("image_type", "RAW8");
            m.put("bin", 2L);
            m.put("width", 0L);
            m.put("height", 0L);
            m.put("start_x", -1L);
            m.put("start_y", -1L);
            m.put("exposure_us", 10000L);
            m.put("auto_exposure", false);
            link.set(m);
            Frame f = waitFrame(frames, maxW / 2, maxH / 2, 10000);
            check("cuadros de " + maxW / 2 + "x" + maxH / 2, f != null,
                    f == null ? "" : "exposicion " + f.exposureUs + " us, " + f.payloadSize + " bytes");
            if (f != null) {
                byte[] g = f.gray8();
                long sum = 0;
                for (byte b : g) sum += b & 0xff;
                System.out.println(String.format("     imagen real: brillo medio %.1f, nitidez %.2f",
                        sum / (double) g.length, Focus.sharpness(g, f.width, f.height)));
                int[] lut = Focus.lut(g, f.width, f.height, true);
                check("tabla de grises estirada", lut.length == 256 && (lut[255] & 0xff) == 255, null);
            }
            // la imagen real puede estar negra (de noche): la nitidez se prueba con bordes sinteticos
            byte[] syn = synthetic(640, 480);
            double s0 = Focus.sharpness(syn, 640, 480);
            double s1 = Focus.sharpness(boxBlur(syn, 640, 480, 1), 640, 480);
            double s2 = Focus.sharpness(boxBlur(syn, 640, 480, 3), 640, 480);
            check("la nitidez baja al desenfocar, y baja mas cuanto mas borroso", s0 > s1 && s1 > s2,
                    String.format("%.1f -> %.1f -> %.1f", s0, s1, s2));
            byte[] darker = new byte[syn.length];
            for (int i = 0; i < syn.length; i++) darker[i] = (byte) ((syn[i] & 0xff) / 2);
            double sd = Focus.sharpness(darker, 640, 480);
            check("casi no cambia con la exposicion (misma imagen, mitad de brillo)", Math.abs(sd - s0) / s0 < 0.15,
                    String.format("%.1f vs %.1f", s0, sd));

            System.out.println("5) Modo Enfoque (recorte central 640x480 a resolucion real)");
            m = new LinkedHashMap<>();
            m.put("bin", 1L);
            m.put("width", 640L);
            m.put("height", 480L);
            m.put("start_x", -1L);
            m.put("start_y", -1L);
            link.set(m);
            f = waitFrame(frames, 640, 480, 10000);
            check("cuadros de 640x480", f != null, null);
            if (f != null) {
                long seq0 = f.seq;
                Thread.sleep(3000);
                Frame last = lastFrame(frames);
                double fps = (last.seq - seq0) / 3.0;
                check("fps del recorte", fps > 5, String.format("%.1f fps, %.0f Mbit/s", fps, fps * 640 * 480 * 8 / 1e6));
                check("nitidez medida en el recorte", Focus.sharpness(last.gray8(), last.width, last.height) > 0, null);
            }

            System.out.println("6) Exposicion y ganancia");
            m = new LinkedHashMap<>();
            m.put("exposure_us", 5000L);
            m.put("gain", 50L);
            m.put("auto_exposure", false);
            m.put("auto_gain", false);
            link.set(m);
            f = null;
            long t1 = System.currentTimeMillis();
            while (System.currentTimeMillis() - t1 < 6000) {
                Frame x = lastFrame(frames);
                if (x != null && x.exposureUs == 5000 && x.gain == 50) {
                    f = x;
                    break;
                }
                Thread.sleep(50);
            }
            check("exposicion 5 ms y ganancia 50 llegan en los cuadros", f != null, null);
        } finally {
            link.stopStream();
            Map<String, Object> back = link.set(original);
            boolean same = true;
            for (String k : original.keySet()) {
                Object x = original.get(k);
                Object y = back.get(k);
                if (x == null ? y != null : !x.equals(y)) {
                    same = false;
                    System.out.println("     distinto: " + k + " " + x + " -> " + y);
                }
            }
            check("ajustes originales restaurados", same, null);
            link.close();
        }
        end();
    }

    static Frame lastFrame(List<Frame> frames) {
        synchronized (frames) {
            return frames.isEmpty() ? null : frames.get(frames.size() - 1);
        }
    }

    static Frame waitFrame(List<Frame> frames, int w, int h, long ms) throws InterruptedException {
        long t0 = System.currentTimeMillis();
        while (System.currentTimeMillis() - t0 < ms) {
            Frame f = lastFrame(frames);
            if (f != null && f.width == w && f.height == h) return f;
            Thread.sleep(50);
        }
        return null;
    }

    /** Escena con bordes: rectangulos de distintos grises (sin aleatoriedad). */
    static byte[] synthetic(int w, int h) {
        byte[] g = new byte[w * h];
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                int v = 60 + ((x / 23) * 37 + (y / 17) * 53) % 150;
                g[y * w + x] = (byte) v;
            }
        }
        return g;
    }

    static byte[] boxBlur(byte[] g, int w, int h, int r) {
        byte[] o = new byte[g.length];
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                int s = 0;
                int n = 0;
                for (int dy = -r; dy <= r; dy++) {
                    int yy = y + dy;
                    if (yy < 0 || yy >= h) continue;
                    for (int dx = -r; dx <= r; dx++) {
                        int xx = x + dx;
                        if (xx < 0 || xx >= w) continue;
                        s += g[yy * w + xx] & 0xff;
                        n++;
                    }
                }
                o[y * w + x] = (byte) (s / n);
            }
        }
        return o;
    }

    static void end() {
        System.out.println("\nRESUMEN: " + total + " pruebas, " + fails + " fallas");
        System.exit(fails == 0 ? 0 : 1);
    }
}
