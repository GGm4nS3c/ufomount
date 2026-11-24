package com.ufomount.foco;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.GestureDetector;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.view.View;

/** Imagen en vivo con zoom de dos dedos, arrastre y doble toque (ajustar / acercar). */
public class ZoomView extends View {
    private Bitmap bmp;
    private String overlay = "";
    private boolean cross = true;
    private Runnable onDrawn;
    private float scale = 1f;      // sobre "ajustar a la pantalla"
    private float tx;              // desplazamiento respecto de la imagen centrada
    private float ty;
    private final Paint img = new Paint();
    private final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint shade = new Paint();
    private final Paint crossPaint = new Paint();
    private final RectF dst = new RectF();
    private final ScaleGestureDetector scaler;
    private final GestureDetector gestures;

    public ZoomView(Context ctx) {
        super(ctx);
        float d = ctx.getResources().getDisplayMetrics().density;
        text.setColor(0xFFE4E9ED);
        text.setTextSize(12 * d);
        text.setTypeface(android.graphics.Typeface.MONOSPACE);
        shade.setColor(0xAA000000);
        crossPaint.setColor(0xCCFF5050);
        crossPaint.setStrokeWidth(Math.max(1f, d));
        scaler = new ScaleGestureDetector(ctx, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override
            public boolean onScale(ScaleGestureDetector det) {
                zoomAround(scale * det.getScaleFactor(), det.getFocusX(), det.getFocusY());
                return true;
            }
        });
        gestures = new GestureDetector(ctx, new GestureDetector.SimpleOnGestureListener() {
            @Override
            public boolean onDown(MotionEvent e) {
                return true;
            }

            @Override
            public boolean onScroll(MotionEvent e1, MotionEvent e2, float dx, float dy) {
                tx -= dx;
                ty -= dy;
                clampPan();
                invalidate();
                return true;
            }

            @Override
            public boolean onDoubleTap(MotionEvent e) {
                if (scale > 1.01f) {
                    scale = 1f;
                    tx = 0;
                    ty = 0;
                    invalidate();
                } else {
                    zoomAround(pixelScale() * 2f, e.getX(), e.getY());
                }
                return true;
            }
        });
    }

    public void setBitmap(Bitmap b, String overlayText) {
        bmp = b;
        overlay = overlayText == null ? "" : overlayText;
        clampPan();
        invalidate();
    }

    public void clear(String overlayText) {
        bmp = null;
        overlay = overlayText == null ? "" : overlayText;
        invalidate();
    }

    public void setCross(boolean on) {
        cross = on;
        invalidate();
    }

    /** Se llama despues de dibujar cada cuadro (para no pisar el bitmap que se muestra). */
    public void setOnDrawn(Runnable r) {
        onDrawn = r;
    }

    private float fit() {
        if (bmp == null || getWidth() == 0 || getHeight() == 0) return 1f;
        return Math.min(getWidth() / (float) bmp.getWidth(), getHeight() / (float) bmp.getHeight());
    }

    /** Escala (sobre "ajustar") a la que un pixel de la camara ocupa un pixel de pantalla. */
    private float pixelScale() {
        return Math.max(1f, 1f / fit());
    }

    private void zoomAround(float ns, float fx, float fy) {
        ns = Math.max(1f, Math.min(ns, 16f * pixelScale()));
        float k = ns / scale;
        float cx = getWidth() / 2f;
        float cy = getHeight() / 2f;
        tx = fx - cx - (fx - cx - tx) * k;
        ty = fy - cy - (fy - cy - ty) * k;
        scale = ns;
        clampPan();
        invalidate();
    }

    private void clampPan() {
        if (bmp == null) return;
        float s = fit() * scale;
        float mx = Math.max(0, (bmp.getWidth() * s - getWidth()) / 2f);
        float my = Math.max(0, (bmp.getHeight() * s - getHeight()) / 2f);
        tx = Math.max(-mx, Math.min(mx, tx));
        ty = Math.max(-my, Math.min(my, ty));
    }

    @Override
    public boolean onTouchEvent(MotionEvent ev) {
        scaler.onTouchEvent(ev);
        gestures.onTouchEvent(ev);
        return true;
    }

    @Override
    protected void onDraw(Canvas c) {
        c.drawColor(Color.BLACK);
        int w = getWidth();
        int h = getHeight();
        if (bmp != null) {
            float s = fit() * scale;
            float dw = bmp.getWidth() * s;
            float dh = bmp.getHeight() * s;
            float ox = (w - dw) / 2f + tx;
            float oy = (h - dh) / 2f + ty;
            dst.set(ox, oy, ox + dw, oy + dh);
            img.setFilterBitmap(s < 1f);   // acercado: pixeles nitidos, sin suavizar
            c.drawBitmap(bmp, null, dst, img);
            if (cross) {
                c.drawLine(w / 2f, 0, w / 2f, h, crossPaint);
                c.drawLine(0, h / 2f, w, h / 2f, crossPaint);
            }
        }
        if (!overlay.isEmpty()) {
            float pad = text.getTextSize() * 0.4f;
            float tw = text.measureText(overlay);
            c.drawRect(0, 0, tw + 2 * pad, text.getTextSize() + 2 * pad, shade);
            c.drawText(overlay, pad, text.getTextSize() + pad * 0.6f, text);
        }
        if (bmp != null && scale > 1.01f) {
            String z = String.format("zoom x%.1f", scale * fit());
            c.drawText(z, w - text.measureText(z) - 8, h - 8, text);
        }
        if (onDrawn != null) onDrawn.run();
    }
}
