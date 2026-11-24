package com.ufomount.foco.core;

/** Ayuda de enfoque y conversion a imagen. */
public final class Focus {
    private Focus() {
    }

    /** Nitidez del centro de la imagen: energia de gradiente normalizada por el brillo.
     *  Sube al acercarse al foco (bordes mas marcados, estrellas mas puntuales) y casi no
     *  cambia con la exposicion. Usa un cuadrado central de hasta 512 px, promediado 2x2
     *  para que el ruido pese menos. */
    public static double sharpness(byte[] g, int w, int h) {
        int side = Math.min(Math.min(w, h), 512) & ~1;
        if (side < 8) return 0;
        int x0 = (w - side) / 2;
        int y0 = (h - side) / 2;
        int n = side / 2;
        float[] d = new float[n * n];
        double sum = 0;
        for (int y = 0; y < n; y++) {
            int r0 = (y0 + 2 * y) * w + x0;
            int r1 = r0 + w;
            for (int x = 0; x < n; x++) {
                int a = r0 + 2 * x;
                int c = r1 + 2 * x;
                float v = ((g[a] & 0xff) + (g[a + 1] & 0xff) + (g[c] & 0xff) + (g[c + 1] & 0xff)) * 0.25f;
                d[y * n + x] = v;
                sum += v;
            }
        }
        double mean = sum / ((double) n * n);
        double e = 0;
        for (int y = 0; y < n - 1; y++) {
            int r = y * n;
            for (int x = 0; x < n - 1; x++) {
                float v = d[r + x];
                float dx = d[r + x + 1] - v;
                float dy = d[r + n + x] - v;
                e += dx * dx + dy * dy;
            }
        }
        e /= (double) (n - 1) * (n - 1);
        return 1000.0 * e / (mean * mean + 25.0);
    }

    /** Tabla de grises (ARGB) estirada entre los percentiles 0,5 % y 99,8 %, como la
     *  vista automatica del PC; con stretch=false, lineal. */
    public static int[] lut(byte[] g, int w, int h, boolean stretch) {
        int lo = 0;
        int hi = 255;
        if (stretch) {
            int[] hist = new int[256];
            int step = Math.max(1, (int) Math.sqrt((double) w * h / 40000.0));
            int count = 0;
            for (int y = 0; y < h; y += step) {
                int r = y * w;
                for (int x = 0; x < w; x += step) {
                    hist[g[r + x] & 0xff]++;
                    count++;
                }
            }
            int a = (int) (count * 0.005);
            int b = (int) (count * 0.998);
            int c = 0;
            lo = -1;
            for (int i = 0; i < 256; i++) {
                c += hist[i];
                if (lo < 0 && c > a) lo = i;
                if (c >= b) {
                    hi = i;
                    break;
                }
            }
            if (lo < 0) lo = 0;
            if (hi - lo < 2) hi = Math.min(255, lo + 2);
        }
        int[] t = new int[256];
        for (int i = 0; i < 256; i++) {
            int v = (int) Math.round((i - lo) * 255.0 / (hi - lo));
            v = v < 0 ? 0 : (v > 255 ? 255 : v);
            t[i] = 0xff000000 | (v << 16) | (v << 8) | v;
        }
        return t;
    }

    public static void toArgb(byte[] g, int[] lut, int[] out) {
        int n = Math.min(g.length, out.length);
        for (int i = 0; i < n; i++) out[i] = lut[g[i] & 0xff];
    }
}
