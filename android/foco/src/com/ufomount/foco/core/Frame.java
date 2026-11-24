package com.ufomount.foco.core;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/** Un cuadro crudo del servidor asicam (cabecera de 48 bytes + pixeles). Ver asicam/README.md. */
public final class Frame {
    public static final int HEADER_SIZE = 48;
    public static final int RAW8 = 0;
    public static final int RAW16 = 2;

    public long seq;
    public double timestamp;
    public long exposureUs;
    public int gain;
    public int width;
    public int height;
    public int bin;
    public int imageType;
    public long dropped;
    public int restarts;
    public int flags;
    public Double temperatureC;
    public int payloadSize;
    public byte[] data;

    public boolean autoExposure() {
        return (flags & 1) != 0;
    }

    public boolean autoGain() {
        return (flags & 2) != 0;
    }

    /** Lee la cabecera "<4sHHIdIHHHBBIHHh2xI" (little-endian). */
    public static Frame parseHeader(byte[] h) throws IOException {
        if (h.length < HEADER_SIZE || h[0] != 'A' || h[1] != 'S' || h[2] != 'I' || h[3] != 'F') {
            throw new IOException("cabecera de cuadro invalida");
        }
        ByteBuffer b = ByteBuffer.wrap(h).order(ByteOrder.LITTLE_ENDIAN);
        int headerSize = b.getShort(6) & 0xffff;
        if (headerSize != HEADER_SIZE) throw new IOException("cabecera de " + headerSize + " bytes no soportada");
        Frame f = new Frame();
        f.seq = b.getInt(8) & 0xffffffffL;
        f.timestamp = b.getDouble(12);
        f.exposureUs = b.getInt(20) & 0xffffffffL;
        f.gain = b.getShort(24) & 0xffff;
        f.width = b.getShort(26) & 0xffff;
        f.height = b.getShort(28) & 0xffff;
        f.bin = h[30] & 0xff;
        f.imageType = h[31] & 0xff;
        f.dropped = b.getInt(32) & 0xffffffffL;
        f.restarts = b.getShort(36) & 0xffff;
        f.flags = b.getShort(38) & 0xffff;
        short t = b.getShort(40);
        f.temperatureC = t == Short.MIN_VALUE ? null : t / 10.0;
        f.payloadSize = b.getInt(44);
        long expected = (long) f.width * f.height * (f.imageType == RAW16 ? 2 : 1);
        if (f.payloadSize < 0 || f.payloadSize != expected) throw new IOException("tamano de cuadro invalido");
        return f;
    }

    /** Pixeles en 8 bits para mostrar y medir: RAW8 tal cual; RAW16 su byte alto. */
    public byte[] gray8() {
        if (imageType != RAW16) return data;
        int n = width * height;
        byte[] g = new byte[n];
        for (int i = 0; i < n; i++) g[i] = data[2 * i + 1];
        return g;
    }
}
