package com.ufomount.foco;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.res.ColorStateList;
import android.graphics.Bitmap;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;

import com.ufomount.foco.core.AsiCamLink;
import com.ufomount.foco.core.Discovery;
import com.ufomount.foco.core.Focus;
import com.ufomount.foco.core.Frame;
import com.ufomount.foco.core.MiniJson;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** UFOMOUNT Foco: encuentra la camara de la Raspberry y ayuda a enfocarla desde el telefono. */
public class MainActivity extends Activity implements AsiCamLink.Listener {
    // los colores de las interfaces del PC
    static final int C_BG = 0xFF14191F;
    static final int C_PANEL2 = 0xFF232C34;
    static final int C_TEXT = 0xFFE4E9ED;
    static final int C_MUTED = 0xFF92A0AC;
    static final int C_ACC = 0xFF64B5F6;
    static final int C_OK = 0xFF7FC698;
    static final int C_WARN = 0xFFD9A93F;
    static final int C_ERR = 0xFFE57373;
    static final int C_SEL = 0xFF2F5878;

    static final double EXP_MIN = 32;          // us
    static final double EXP_MAX = 2000000;     // 2 s
    static final int MODE_FULL = 0;            // imagen completa, bin 2
    static final int MODE_FOCUS = 1;           // centro a resolucion real

    private final ExecutorService ctrl = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private SharedPreferences prefs;
    private float dp;

    private volatile AsiCamLink link;          // se asigna en el hilo de la interfaz
    private Map<String, Object> original;      // ajustes de la camara antes de conectar
    private boolean visible;
    private int mode = MODE_FULL;
    private boolean updating;                  // cargando valores en los controles
    private Runnable pendingSet;

    private EditText hostEdit;
    private Button findBtn;
    private Button connBtn;
    private Button fullBtn;
    private Button focusBtn;
    private TextView status;
    private TextView focusVal;
    private TextView focusPeak;
    private TextView expLbl;
    private TextView gainLbl;
    private SeekBar expBar;
    private SeekBar gainBar;
    private CheckBox expAuto;
    private CheckBox gainAuto;
    private ZoomView view;
    private FocusBar bar;

    // cuadros (hilo de datos)
    private final Bitmap[] bmps = new Bitmap[2];
    private int bmpIdx;
    private int[] argb;
    private int[] lut;
    private int lutCount;
    private volatile boolean drawPending;
    private volatile long pendingSince;
    private volatile double focusNow = -1;
    private volatile double focusMax;
    private long fpsT0;
    private int fpsN;
    private volatile double fps;

    // ------------------------------------------------------------------ ciclo de vida
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        dp = getResources().getDisplayMetrics().density;
        prefs = getSharedPreferences("foco", Context.MODE_PRIVATE);
        mode = prefs.getInt("mode", MODE_FULL);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(buildUi());
        hostEdit.setText(prefs.getString("host", ""));
        updateModeButtons();
        view.clear("Busca la Raspberry o escribe su IP");
    }

    @Override
    protected void onStart() {
        super.onStart();
        visible = true;
        if (link == null) {
            String h = prefs.getString("host", "");
            if (!h.isEmpty()) connect(h, true);   // si no responde, la busca
            else find();
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        visible = false;
        disconnect(true, "En pausa: vuelve a la app para seguir");
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ctrl.shutdown();
    }

    // ------------------------------------------------------------------ interfaz
    private int px(float v) {
        return Math.round(v * dp);
    }

    private TextView text(String t, float sp, int color, boolean bold) {
        TextView v = new TextView(this);
        v.setText(t);
        v.setTextSize(sp);
        v.setTextColor(color);
        if (bold) v.setTypeface(v.getTypeface(), android.graphics.Typeface.BOLD);
        return v;
    }

    private LinearLayout row() {
        LinearLayout r = new LinearLayout(this);
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setGravity(Gravity.CENTER_VERTICAL);
        r.setPadding(0, px(4), 0, px(4));
        return r;
    }

    private Button button(String t, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(t);
        b.setAllCaps(false);
        b.setTextColor(C_TEXT);
        b.setTextSize(14);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(C_PANEL2);
        bg.setCornerRadius(px(6));
        b.setBackground(bg);
        b.setPadding(px(12), px(6), px(12), px(6));
        b.setMinHeight(px(40));
        b.setMinimumHeight(px(40));
        b.setOnClickListener(l);
        return b;
    }

    private LinearLayout.LayoutParams gap(float weight) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                weight > 0 ? 0 : ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, weight);
        p.setMargins(px(3), 0, px(3), 0);
        return p;
    }

    private CheckBox check(String t, boolean on, CompoundButton.OnCheckedChangeListener l) {
        CheckBox c = new CheckBox(this);
        c.setText(t);
        c.setTextColor(C_TEXT);
        c.setButtonTintList(ColorStateList.valueOf(C_ACC));
        c.setChecked(on);
        c.setOnCheckedChangeListener(l);
        return c;
    }

    private SeekBar seek(int max, SeekBar.OnSeekBarChangeListener l) {
        SeekBar s = new SeekBar(this);
        s.setMax(max);
        s.setProgressTintList(ColorStateList.valueOf(C_ACC));
        s.setThumbTintList(ColorStateList.valueOf(C_TEXT));
        s.setOnSeekBarChangeListener(l);
        s.setPadding(px(12), px(6), px(12), px(6));
        return s;
    }

    private View buildUi() {
        final LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(C_BG);
        final int pad = px(8);
        root.setPadding(pad, pad, pad, pad);
        // Android 15 dibuja bajo las barras del sistema: dejar su espacio
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override
            public WindowInsets onApplyWindowInsets(View v, WindowInsets ins) {
                int l;
                int t;
                int r;
                int b;
                if (Build.VERSION.SDK_INT >= 30) {
                    android.graphics.Insets s = ins.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                    l = s.left;
                    t = s.top;
                    r = s.right;
                    b = s.bottom;
                } else {
                    l = ins.getSystemWindowInsetLeft();
                    t = ins.getSystemWindowInsetTop();
                    r = ins.getSystemWindowInsetRight();
                    b = ins.getSystemWindowInsetBottom();
                }
                v.setPadding(l + pad, t + pad, r + pad, b + pad);
                return ins;
            }
        });

        root.addView(text("UFOMOUNT Foco", 18, C_TEXT, true));

        LinearLayout r1 = row();
        hostEdit = new EditText(this);
        hostEdit.setHint("IP o red, p. ej. 192.168.201.0");
        hostEdit.setSingleLine(true);
        hostEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostEdit.setTextColor(C_TEXT);
        hostEdit.setHintTextColor(C_MUTED);
        hostEdit.setTextSize(15);
        hostEdit.setImeOptions(EditorInfo.IME_ACTION_SEARCH);
        hostEdit.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override
            public boolean onEditorAction(TextView v, int action, android.view.KeyEvent e) {
                find();
                return true;
            }
        });
        r1.addView(hostEdit, gap(1));
        findBtn = button("Buscar", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                find();
            }
        });
        r1.addView(findBtn, gap(0));
        connBtn = button("Conectar", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                toggleConnection();
            }
        });
        r1.addView(connBtn, gap(0));
        root.addView(r1);

        status = text("", 13, C_MUTED, false);
        root.addView(status);

        view = new ZoomView(this);
        view.setOnDrawn(new Runnable() {
            @Override
            public void run() {
                drawPending = false;
            }
        });
        LinearLayout.LayoutParams vp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1);
        vp.setMargins(0, px(4), 0, px(4));
        root.addView(view, vp);

        LinearLayout r2 = row();
        r2.addView(text("Nitidez", 13, C_MUTED, false));
        focusVal = text("—", 28, C_TEXT, true);
        focusVal.setPadding(px(10), 0, px(10), 0);
        focusVal.setSingleLine(true);
        r2.addView(focusVal);
        focusPeak = text("", 13, C_MUTED, false);
        focusPeak.setSingleLine(true);
        r2.addView(focusPeak, gap(1));
        r2.addView(button("Reiniciar máx.", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                focusMax = Math.max(0, focusNow);
                updateFocusUi();
            }
        }), gap(0));
        root.addView(r2);

        bar = new FocusBar(this);
        root.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, px(14)));
        root.addView(text("Gira el anillo de enfoque hasta que la nitidez llegue a su máximo (barra verde). "
                + "Dos dedos: zoom · doble toque: acercar.", 12, C_MUTED, false));

        LinearLayout r3 = row();
        fullBtn = button("Completa", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                setMode(MODE_FULL);
            }
        });
        focusBtn = button("Enfoque (centro 1:1)", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                setMode(MODE_FOCUS);
            }
        });
        r3.addView(fullBtn, gap(1));
        r3.addView(focusBtn, gap(1));
        r3.addView(check("Cruz", true, new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton c, boolean on) {
                view.setCross(on);
            }
        }), gap(0));
        root.addView(r3);

        LinearLayout r4 = row();
        expLbl = text("Exposición —", 14, C_TEXT, false);
        r4.addView(expLbl, gap(1));
        expAuto = check("Auto", false, new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton c, boolean on) {
                expBar.setEnabled(!on);
                if (!updating) sendSet(map("auto_exposure", on));
            }
        });
        r4.addView(expAuto, gap(0));
        root.addView(r4);
        expBar = seek(1000, new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar s, int p, boolean user) {
                long us = posToExp(p);
                expLbl.setText("Exposición " + fmtExp(us));
                if (user && !updating) {
                    Map<String, Object> m = map("exposure_us", us);
                    m.put("auto_exposure", false);
                    debounceSet(m);
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar s) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar s) {
            }
        });
        root.addView(expBar);

        LinearLayout r5 = row();
        gainLbl = text("Ganancia —", 14, C_TEXT, false);
        r5.addView(gainLbl, gap(1));
        gainAuto = check("Auto", false, new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton c, boolean on) {
                gainBar.setEnabled(!on);
                if (!updating) sendSet(map("auto_gain", on));
            }
        });
        r5.addView(gainAuto, gap(0));
        root.addView(r5);
        gainBar = seek(600, new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar s, int p, boolean user) {
                gainLbl.setText("Ganancia " + p);
                if (user && !updating) {
                    Map<String, Object> m = map("gain", (long) p);
                    m.put("auto_gain", false);
                    debounceSet(m);
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar s) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar s) {
            }
        });
        root.addView(gainBar);
        return root;
    }

    private void setStatus(String t, int color) {
        status.setText(t);
        status.setTextColor(color);
    }

    private void hideKeyboard() {
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null) imm.hideSoftInputFromWindow(hostEdit.getWindowToken(), 0);
        hostEdit.clearFocus();
    }

    private void updateModeButtons() {
        ((GradientDrawable) fullBtn.getBackground()).setColor(mode == MODE_FULL ? C_SEL : C_PANEL2);
        ((GradientDrawable) focusBtn.getBackground()).setColor(mode == MODE_FOCUS ? C_SEL : C_PANEL2);
    }

    // ------------------------------------------------------------------ valores
    static long posToExp(int p) {
        return Math.round(EXP_MIN * Math.pow(EXP_MAX / EXP_MIN, p / 1000.0));
    }

    static int expToPos(long us) {
        double v = Math.log(Math.max(EXP_MIN, Math.min(EXP_MAX, us)) / EXP_MIN) / Math.log(EXP_MAX / EXP_MIN);
        return (int) Math.round(v * 1000);
    }

    static String fmtExp(long us) {
        if (us < 1000) return us + " µs";
        if (us < 100000) return String.format("%.1f ms", us / 1000.0);
        if (us < 1000000) return String.format("%.0f ms", us / 1000.0);
        return String.format("%.2f s", us / 1e6);
    }

    /** El indice x1000, con tres cifras significativas: de noche es muy chico (0,000675) y
     *  sin escalar no cabian a la vez el valor y el maximo en la fila. Es relativo, asi que
     *  la escala da lo mismo. */
    static String fmtFocus(double v) {
        v *= 1000;
        if (v <= 0) return "0";
        if (v >= 100) return String.format("%.0f", v);
        int decimals = Math.min(4, Math.max(0, 2 - (int) Math.floor(Math.log10(v))));
        return String.format("%." + decimals + "f", v);
    }

    static Map<String, Object> map(String k, Object v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(k, v);
        return m;
    }

    private void syncControls(Map<String, Object> s) {
        if (s == null) return;
        updating = true;
        long exp = MiniJson.getLong(s, "exposure_us", 10000);
        boolean ae = MiniJson.getBool(s, "auto_exposure");
        long gain = MiniJson.getLong(s, "gain", 0);
        boolean ag = MiniJson.getBool(s, "auto_gain");
        expAuto.setChecked(ae);
        expBar.setEnabled(!ae);
        expBar.setProgress(expToPos(exp));
        expLbl.setText("Exposición " + fmtExp(exp));
        gainAuto.setChecked(ag);
        gainBar.setEnabled(!ag);
        gainBar.setProgress((int) gain);
        gainLbl.setText("Ganancia " + gain);
        updating = false;
    }

    private void showAuto(boolean ae, long exp, boolean ag, int gain) {
        updating = true;
        if (ae) {
            expBar.setProgress(expToPos(exp));
            expLbl.setText("Exposición " + fmtExp(exp) + " (auto)");
        }
        if (ag) {
            gainBar.setProgress(gain);
            gainLbl.setText("Ganancia " + gain + " (auto)");
        }
        updating = false;
    }

    private void updateFocusUi() {
        double v = focusNow;
        double m = focusMax;
        if (v < 0) {
            focusVal.setText("—");
            focusPeak.setText("");
            focusVal.setTextColor(C_TEXT);
            bar.set(0, false);
            return;
        }
        double frac = m > 0 ? v / m : 0;
        boolean best = frac >= 0.97;
        focusVal.setText(fmtFocus(v));
        focusVal.setTextColor(best ? C_OK : C_TEXT);
        focusPeak.setText("máx " + fmtFocus(m));
        bar.set((float) frac, best);
    }

    // ------------------------------------------------------------------ red
    private static boolean looksLikeNetwork(String t) {
        return t.endsWith(".0") || t.endsWith(".x") || t.endsWith("/24") || t.split("\\.").length == 3;
    }

    private void find() {
        hideKeyboard();
        findBtn.setEnabled(false);
        setStatus("Buscando la Raspberry…", C_MUTED);
        final List<String> targets = new ArrayList<>();
        String t = hostEdit.getText().toString().trim();
        if (!t.isEmpty()) targets.add(t);
        String last = prefs.getString("host", "");
        if (!last.isEmpty() && !last.equals(t)) targets.add(last);
        new Thread(new Runnable() {
            @Override
            public void run() {
                final List<Discovery.Server> res = Discovery.find(targets, 1500, true);
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        findBtn.setEnabled(true);
                        if (res.isEmpty()) {
                            setStatus("No encontré la Raspberry. Si está en otra red, escribe una IP de esa red "
                                    + "(p. ej. 192.168.201.0) y pulsa Buscar.", C_WARN);
                            return;
                        }
                        Discovery.Server s = res.get(0);
                        hostEdit.setText(s.ip);
                        prefs.edit().putString("host", s.ip).apply();
                        if (s.camera == null) {
                            setStatus("Encontrada: " + s.host + " (" + s.ip + "), pero no tiene la cámara conectada.", C_WARN);
                            return;
                        }
                        setStatus("Encontrada: " + s, C_OK);
                        connect(s.ip, false);
                    }
                });
            }
        }, "buscar").start();
    }

    private void toggleConnection() {
        if (link != null) {
            disconnect(true, "Desconectado");
            return;
        }
        String t = hostEdit.getText().toString().trim();
        if (t.isEmpty() || looksLikeNetwork(t)) find();
        else connect(t, false);
    }

    private static Map<String, Object> applyMode(AsiCamLink l, int mode) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("image_type", "RAW8");
        if (mode == MODE_FOCUS) {
            m.put("bin", 1L);
            m.put("width", 640L);
            m.put("height", 480L);
        } else {
            m.put("bin", 2L);
            m.put("width", 0L);
            m.put("height", 0L);
        }
        m.put("start_x", -1L);
        m.put("start_y", -1L);
        return l.set(m);
    }

    /** @param findIfFails al abrir la app con la ultima IP guardada: si no responde (otra red,
     *                     la Pi cambio de IP), buscarla en vez de quedarse en el error. */
    private void connect(final String host, final boolean findIfFails) {
        disconnect(true, null);
        hideKeyboard();
        setStatus("Conectando a " + host + "…", C_WARN);
        view.clear("Conectando…");
        final int m = mode;
        ctrl.execute(new Runnable() {
            @Override
            public void run() {
                final AsiCamLink l = new AsiCamLink(host);
                try {
                    final Map<String, Object> h = l.hello();
                    Map<String, Object> inf;
                    try {
                        inf = l.info();
                    } catch (AsiCamLink.ServerError e) {
                        throw new IOException("la Raspberry no tiene la cámara conectada");
                    }
                    final Map<String, Object> cam = MiniJson.getMap(inf, "camera");
                    Map<String, Object> gainC = MiniJson.getMap(MiniJson.getMap(inf, "controls"), "gain");
                    final long gmax = gainC != null ? MiniJson.getLong(gainC, "max", 600) : 600;
                    // si la vez anterior la app se cerro sin devolver los ajustes (la mato el
                    // sistema, se corto la red...), los de ahora son los de la app: se usan
                    // los guardados. Si no, se guardan hasta haberlos devuelto.
                    Map<String, Object> saved = loadPendingOriginal(host);
                    final Map<String, Object> orig = saved != null ? saved : l.settings();
                    if (saved == null) savePendingOriginal(host, orig);
                    final Map<String, Object> now = applyMode(l, m);
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            if (!visible || isFinishing() || link != null) {
                                restoreAndClose(l, orig);   // la app ya no esta a la vista
                                return;
                            }
                            link = l;
                            original = orig;
                            gainBar.setMax((int) gmax);
                            prefs.edit().putString("host", host).apply();
                            hostEdit.setText(host);
                            setStatus("Conectado a " + MiniJson.getString(h, "host", host) + " · "
                                    + MiniJson.getString(cam, "name", "cámara"), C_OK);
                            connBtn.setText("Desconectar");
                            syncControls(now);
                            focusNow = -1;
                            focusMax = 0;
                            fpsT0 = 0;
                            drawPending = false;
                            l.startStream(MainActivity.this);
                        }
                    });
                } catch (final IOException e) {
                    l.close();
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            if (findIfFails && visible && link == null) {
                                find();
                                return;
                            }
                            setStatus("No se pudo conectar a " + host + ": " + e.getMessage(), C_ERR);
                            view.clear("Sin conexión");
                        }
                    });
                }
            }
        });
    }

    // ajustes de la camara antes de la app, guardados hasta devolverlos (SharedPreferences
    // es seguro entre hilos)
    private static final String P_ORIG = "ajustes_originales";
    private static final String P_ORIG_HOST = "ajustes_originales_ip";

    @SuppressWarnings("unchecked")
    private Map<String, Object> loadPendingOriginal(String host) {
        if (!host.equals(prefs.getString(P_ORIG_HOST, ""))) return null;
        try {
            Object o = MiniJson.parse(prefs.getString(P_ORIG, ""));
            return o instanceof Map ? (Map<String, Object>) o : null;
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private void savePendingOriginal(String host, Map<String, Object> s) {
        if (s == null) return;
        prefs.edit().putString(P_ORIG_HOST, host).putString(P_ORIG, MiniJson.stringify(s)).commit();
    }

    private void clearPendingOriginal() {
        prefs.edit().remove(P_ORIG_HOST).remove(P_ORIG).apply();
    }

    private void restoreAndClose(final AsiCamLink l, final Map<String, Object> orig) {
        ctrl.execute(new Runnable() {
            @Override
            public void run() {
                l.stopStream();
                if (orig != null) {
                    try {
                        l.set(orig);   // la camara queda como estaba (para el PC)
                        clearPendingOriginal();
                    } catch (IOException ignored) {
                        // la Pi no responde: quedan guardados y se devuelven al reconectar
                    }
                }
                l.close();
            }
        });
    }

    private void disconnect(boolean restore, String msg) {
        AsiCamLink l = link;
        Map<String, Object> orig = original;
        link = null;
        original = null;
        if (pendingSet != null) {
            ui.removeCallbacks(pendingSet);
            pendingSet = null;
        }
        connBtn.setText("Conectar");
        if (msg != null) {
            setStatus(msg, C_MUTED);
            view.clear(msg);
        }
        focusNow = -1;
        updateFocusUi();
        if (l != null) restoreAndClose(l, restore ? orig : null);
    }

    private void debounceSet(final Map<String, Object> changes) {
        if (pendingSet != null) ui.removeCallbacks(pendingSet);
        pendingSet = new Runnable() {
            @Override
            public void run() {
                pendingSet = null;
                sendSet(changes);
            }
        };
        ui.postDelayed(pendingSet, 200);
    }

    private void sendSet(final Map<String, Object> changes) {
        final AsiCamLink l = link;
        if (l == null) return;
        ctrl.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    l.set(changes);
                } catch (final IOException e) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            setStatus("No se aplicó el ajuste: " + e.getMessage(), C_ERR);
                        }
                    });
                }
            }
        });
    }

    private void setMode(final int m) {
        mode = m;
        prefs.edit().putInt("mode", m).apply();
        updateModeButtons();
        focusNow = -1;
        focusMax = 0;
        updateFocusUi();
        final AsiCamLink l = link;
        if (l == null) return;
        ctrl.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    applyMode(l, m);
                } catch (final IOException e) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            setStatus("No se pudo cambiar el modo: " + e.getMessage(), C_ERR);
                        }
                    });
                }
            }
        });
    }

    // ------------------------------------------------------------------ cuadros (hilo de datos)
    @Override
    public void onFrame(Frame f) {
        if (link == null) return;
        byte[] g = f.gray8();
        int w = f.width;
        int h = f.height;
        double s = Focus.sharpness(g, w, h);
        focusNow = focusNow < 0 ? s : focusNow * 0.6 + s * 0.4;   // suaviza el ruido
        if (focusNow > focusMax) focusMax = focusNow;
        long now = System.currentTimeMillis();
        if (fpsT0 == 0) fpsT0 = now;
        fpsN++;
        if (now - fpsT0 >= 1000) {
            fps = fpsN * 1000.0 / (now - fpsT0);
            fpsN = 0;
            fpsT0 = now;
        }
        // la pantalla no alcanza a dibujar todo: se salta el cuadro (la nitidez si se mide)
        if (drawPending && now - pendingSince < 1000) return;
        if (lut == null || (lutCount++ % 5) == 0) lut = Focus.lut(g, w, h, true);
        if (argb == null || argb.length != w * h) argb = new int[w * h];
        Focus.toArgb(g, lut, argb);
        Bitmap bm = bmps[bmpIdx];
        if (bm == null || bm.getWidth() != w || bm.getHeight() != h) {
            bm = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
            bmps[bmpIdx] = bm;
        }
        bm.setPixels(argb, 0, w, 0, 0, w, h);
        bmpIdx ^= 1;
        final Bitmap show = bm;
        final String ov = String.format("%dx%d bin%d · %s · gan %d · %.1f fps", w, h, f.bin,
                fmtExp(f.exposureUs), f.gain, fps);
        final boolean ae = f.autoExposure();
        final boolean ag = f.autoGain();
        final long fe = f.exposureUs;
        final int fg = f.gain;
        drawPending = true;
        pendingSince = now;
        ui.post(new Runnable() {
            @Override
            public void run() {
                if (link == null) {
                    drawPending = false;
                    return;
                }
                view.setBitmap(show, ov);
                updateFocusUi();
                if (ae || ag) showAuto(ae, fe, ag, fg);
            }
        });
    }

    @Override
    public void onStream(final boolean connected, final String detail) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                if (link == null) return;
                if (!connected) setStatus("Sin video (" + detail + "): reintentando…", C_WARN);
            }
        });
    }
}
