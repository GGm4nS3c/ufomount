package com.ufomount.foco;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.View;

/** Barra de nitidez: llena = el maximo alcanzado; verde cuando se esta en el foco. */
public class FocusBar extends View {
    private float frac;
    private boolean best;
    private final Paint back = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF r = new RectF();

    public FocusBar(Context ctx) {
        super(ctx);
        back.setColor(0xFF232C34);
    }

    public void set(float fraction, boolean atBest) {
        frac = Math.max(0f, Math.min(1f, fraction));
        best = atBest;
        invalidate();
    }

    @Override
    protected void onDraw(Canvas c) {
        float rad = getHeight() / 2f;
        r.set(0, 0, getWidth(), getHeight());
        c.drawRoundRect(r, rad, rad, back);
        if (frac > 0) {
            fill.setColor(best ? 0xFF7FC698 : 0xFF64B5F6);
            r.set(0, 0, getWidth() * frac, getHeight());
            c.drawRoundRect(r, rad, rad, fill);
        }
    }
}
