"""
Solar Tracker Selector - Streamlit web app.
Enter a latitude and longitude; the app downloads NASA POWER hourly data, simulates a fixed, a
single-axis and a dual-axis panel, and recommends the best option for that location.

Run:  streamlit run app.py
"""
import datetime as dt
import io

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

import engine as en

CONFIGS = en.CONFIGS
COLORS = {'Fixed': '#8a8a8a', 'Single-axis': '#2a7fde', 'Dual-axis': '#e08a1e'}
plt.rcParams['axes.grid'] = True
plt.rcParams['grid.alpha'] = 0.3

st.set_page_config(page_title='Solar Tracker Selector', page_icon='☀️', layout='wide')


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def show(fig):
    st.pyplot(fig)
    plt.close(fig)


def fmt_table(df, digits=1):
    return df.style.format(lambda v: f'{v:,.{digits}f}' if isinstance(v, (int, float, np.floating)) and not pd.isna(v) else '–')


def make_excel(tables: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as xl:
        for name, df in tables.items():
            df.to_excel(xl, sheet_name=name[:31])
    return buf.getvalue()


# ----------------------------------------------------------------------------
# sidebar: all inputs
# ----------------------------------------------------------------------------
with st.sidebar:
    st.title('☀️ Solar Tracker Selector')
    st.caption('Fixed vs single-axis vs dual-axis: which suits your location?')

    st.header('1. Location')
    lat = st.number_input('Latitude (° N, south is negative)', -90.0, 90.0, 6.93, 0.01, format='%.4f')
    lon = st.number_input('Longitude (° E, west is negative)', -180.0, 180.0, 79.85, 0.01, format='%.4f')

    st.header('2. Weather data')
    source = st.radio('Source', ['NASA POWER (automatic download)', 'Upload NASA POWER CSV'])
    use_api = source.startswith('NASA')
    uploads, tz_label, clock_off = [], None, 5.5
    if use_api:
        last_year = dt.date.today().year - 1
        y0, y1 = st.slider('Years to analyse', 2001, last_year, (max(2001, last_year - 10), last_year))
        st.caption('Each year takes roughly 5-30 s to download. More years give a more reliable answer.')
    else:
        y0 = y1 = None
        uploads = st.file_uploader('CSV file(s) from NASA POWER', type='csv', accept_multiple_files=True)
        tz_label = st.selectbox('Time stamps in the file are',
                                ['NASA local solar time (default NASA download)', 'UTC', 'Local clock time'])
        if tz_label == 'Local clock time':
            clock_off = st.number_input('UTC offset of the clock (hours)', -12.0, 14.0, 5.5, 0.5)

    with st.expander('3. Panel and model settings'):
        opt_fixed = st.checkbox('Optimise the fixed tilt automatically', True,
                                help='A fair comparison needs the best possible fixed panel.')
        fixed_tilt_in = st.slider('Fixed tilt (°), used if not optimising', 0, 60, 10, disabled=opt_fixed)
        max_angle = st.slider('Single-axis rotation limit (±°)', 30, 90, 60)
        transposition = st.selectbox('Transposition model', ['haydavies', 'reindl', 'klucher', 'isotropic'])
        albedo = st.number_input('Ground reflectance (albedo)', 0.0, 1.0, 0.2, 0.05)
        sys_loss = st.number_input('System losses (%)', 0.0, 40.0, 14.0, 1.0,
                                   help='Soiling, wiring, mismatch... PVWatts default is 14 %.')
        gamma = st.number_input('Power temperature coefficient (%/°C)', -1.0, 0.0, -0.40, 0.05)
        use_iam = st.checkbox('Include angle-of-incidence (glass reflection) loss', True)
        default_wind = st.number_input('Wind speed if missing (m/s)', 0.0, 20.0, 1.0, 0.5)
        shift_min = st.slider('Sun position offset within the hour (min)', 0, 60, 30, 5,
                              help='Hourly data are averages; 30 min evaluates the sun at mid-hour.')
        do_sens = st.checkbox('Include sensitivity studies (slower)', True)

    with st.expander('4. Economics (placeholders: replace with real values)'):
        currency = st.text_input('Currency label', 'LKR')
        tariff = st.number_input(f'Electricity value ({currency}/kWh)', 0.0, 10000.0, 30.0, 1.0)
        lifetime = st.slider('Project lifetime (years)', 10, 40, 25)
        discount = st.number_input('Discount rate (%)', 0.0, 30.0, 10.0, 0.5) / 100
        degr = st.number_input('Panel degradation (%/yr)', 0.0, 3.0, 0.5, 0.1) / 100
        tariff_esc = st.number_input('Tariff escalation (%/yr)', -5.0, 15.0, 0.0, 0.5) / 100
        om_esc = st.number_input('O&M cost escalation (%/yr)', 0.0, 15.0, 3.0, 0.5) / 100
        repl_year = st.slider('Tracker motor replacement year', 3, 25, 10)
        st.markdown('**Per kWp installed**')
        capex, om, repl, trk = {}, {}, {}, {}
        defaults = {'Fixed': (200000, 1500, 0, 0), 'Single-axis': (240000, 2500, 8000, 15),
                    'Dual-axis': (290000, 4000, 16000, 30)}
        for c in CONFIGS:
            st.markdown(f'*{c}*')
            a, b = st.columns(2)
            capex[c] = a.number_input('CAPEX', 0.0, 1e9, float(defaults[c][0]), 5000.0, key=f'cx{c}')
            om[c] = b.number_input('O&M / yr', 0.0, 1e8, float(defaults[c][1]), 500.0, key=f'om{c}')
            if c != 'Fixed':
                a, b = st.columns(2)
                repl[c] = a.number_input('Replacement', 0.0, 1e8, float(defaults[c][2]), 1000.0, key=f'rp{c}')
                trk[c] = b.number_input('Motor kWh/yr', 0.0, 1000.0, float(defaults[c][3]), 5.0, key=f'tk{c}')
            else:
                repl[c], trk[c] = 0.0, 0.0

    run = st.button('▶ Run analysis', type='primary')

ec = en.Econ(currency=currency, lifetime=lifetime, discount=discount, degradation=degr, tariff=tariff,
             tariff_esc=tariff_esc, om_esc=om_esc, repl_year=repl_year, capex=capex, om=om, repl=repl,
             tracker_kwh=trk)

sim_sig = (round(lat, 4), round(lon, 4), use_api, y0, y1, tuple(sorted(f.name for f in uploads)) if uploads else (),
           tz_label, clock_off, opt_fixed, fixed_tilt_in, max_angle, transposition, albedo, sys_loss, gamma,
           use_iam, default_wind, shift_min, do_sens)

# ----------------------------------------------------------------------------
# run the simulation (heavy part, only when the button is pressed)
# ----------------------------------------------------------------------------
if run:
    try:
        with st.status('Running analysis...', expanded=True) as status:
            if use_api:
                key = (round(lat, 4), round(lon, 4), y0, y1)
                cache = st.session_state.setdefault('raw_cache', {})
                if key not in cache:
                    bar = st.progress(0.0, text='Contacting NASA POWER...')

                    def prog(i, n, y):
                        bar.progress(min(i / n, 1.0), text=f'Downloading NASA POWER data for {y}...' if y else 'Download complete')
                    cache[key] = en.fetch_nasa(lat, lon, y0, y1, prog)
                raw, elev = cache[key]
                source_text = f'NASA POWER hourly data {y0}-{y1} (UTC), downloaded for {lat:.3f}, {lon:.3f}'
            else:
                if not uploads:
                    raise en.DataError('Please upload at least one CSV file, or switch to automatic NASA POWER download.')
                parts = [en.read_power_csv_text(f.getvalue().decode('utf-8', errors='ignore')) for f in uploads]
                df = pd.concat(parts)
                df = df[~df.index.duplicated(keep='first')].sort_index()
                mode = 'LST' if tz_label.startswith('NASA') else ('UTC' if tz_label == 'UTC' else 'CLOCK')
                raw = en.to_utc(df, mode, lon, clock_off)
                elev = None
                source_text = f'Uploaded file(s): {", ".join(f.name for f in uploads)}'
            st.write('Cleaning and checking the data...')
            W, cov, years = en.prepare_weather(raw, default_wind)
            st.write(f'Using {len(years)} year(s): {years[0]}-{years[-1]}. Simulating the three configurations...')
            stt = en.Settings(fixed_tilt=None if opt_fixed else float(fixed_tilt_in), max_angle=float(max_angle),
                              albedo=albedo, transposition=transposition, gamma=gamma / 100,
                              system_losses=1 - sys_loss / 100, use_iam=use_iam,
                              shift_min=int(shift_min + W.attrs.get('frac_shift_min', 0)))
            sim = en.Sim(W, lat, lon, elev or 0.0, stt)
            R = sim.run()
            sens = {}
            if do_sens:
                st.write('Running sensitivity studies...')
                sens['max_angle'] = en.sens_max_angle(sim)
                sens['models'] = en.sens_models(sim, R)
                sens['iam'] = en.sens_iam(sim, R)
                sens['shift'] = en.sens_shift(sim)
                sens['closure'] = en.closure_test(W, lat, lon, elev or 0.0)
            # model-uncertainty range for the tracker gain (anisotropic models), used in Monte Carlo
            gain_range = (0.85, 1.05)
            if 'models' in sens:
                m = sens['models'].drop(index='isotropic', errors='ignore')
                base = sens['models'].loc[transposition] if transposition in sens['models'].index else None
                if base is not None and len(m):
                    ratios = []
                    for col in ['Single-axis gain (%)', 'Dual-axis gain (%)']:
                        if base[col] > 0.5:
                            ratios += list(m[col] / base[col])
                    if ratios:
                        gain_range = (max(0.5, min(ratios) - 0.05), max(ratios) + 0.05)
            st.session_state['res'] = dict(sig=sim_sig, sim=sim, R=R, W=W, cov=cov, years=years, sens=sens,
                                           gain_range=gain_range, source_text=source_text, lat=lat, lon=lon,
                                           elev=elev)
            status.update(label='Analysis complete', state='complete', expanded=False)
    except en.DataError as e:
        st.error(f'**Data problem:** {e}')
        st.stop()
    except Exception as e:                                   # noqa: BLE001
        st.error(f'Something went wrong: {e}')
        st.stop()

res = st.session_state.get('res')

# ----------------------------------------------------------------------------
# landing page
# ----------------------------------------------------------------------------
if res is None:
    st.title('☀️ Solar Tracker Selector')
    st.markdown("""
**Which is best for your location: a fixed panel, a single-axis tracker, or a dual-axis tracker?**

1. Enter the **latitude and longitude** in the sidebar.
2. Choose the years of weather data (downloaded automatically from NASA POWER) or upload a NASA CSV.
3. Adjust the cost assumptions (they are placeholders until you replace them with real quotes).
4. Press **Run analysis**.

The app simulates all three options hour by hour, over many years, compares their energy yield, and
recommends one based on the criterion you choose: lifetime value (NPV), cost of energy (LCOE), or energy.
""")
    st.map(pd.DataFrame({'lat': [lat], 'lon': [lon]}))
    st.stop()

sim, R, W, cov, years, sens = res['sim'], res['R'], res['W'], res['cov'], res['years'], res['sens']
if res['sig'] != sim_sig:
    st.info('Inputs in the sidebar changed since the last run. Economic inputs update the results immediately, '
            'but location, data and panel/model settings need **Run analysis** again.')

# live economics
E_mean = R.E_year.mean()
tbl, net, cum_curves = en.econ_table(E_mean, ec)
mc_npv, mc_lcoe = en.monte_carlo(E_mean, ec, gain_range=res['gain_range'])

st.title('☀️ Solar Tracker Selector')
st.caption(f"Location {res['lat']:.3f}°, {res['lon']:.3f}°  |  {res['source_text']}  |  {len(years)} year(s) simulated  "
           f"|  fixed baseline: tilt {R.fixed_tilt:.0f}°, azimuth {R.fixed_az:.0f}°")

crit_label = st.radio('Choose the best option by', list(en.CRITERIA.keys()), horizontal=True)
rec = en.recommend(tbl, R.E_year, mc_npv, mc_lcoe, ec, en.CRITERIA[crit_label])
best = rec['best']
icon = {'Fixed': '🟫', 'Single-axis': '🟦', 'Dual-axis': '🟧'}[best]

# ---- recommendation banner ----
left, right = st.columns([3, 2])
with left:
    st.success(f'## {icon} Recommended: {best}')
    for line in en.explanation(best, rec, tbl, R, ec):
        st.markdown('- ' + line)
    if rec['close_call']:
        st.warning('**Close call.** The best option changes with the cost and tariff assumptions, so the economic '
                   'inputs decide this result. Replace the placeholder costs with real quotes before relying on it.')
    for n in rec['notes']:
        st.warning(n)
with right:
    cols = st.columns(3)
    for col, c in zip(cols, CONFIGS):
        col.metric(c, f"{E_mean[c]:,.0f} kWh/kWp", f"{(E_mean[c] / E_mean['Fixed'] - 1) * 100:+.1f}%" if c != 'Fixed' else None)
    if rec['probs'] is not None:
        st.caption('Share of random scenarios in which each option is best')
        st.bar_chart(rec['probs'].rename('Share'))

st.subheader('Scorecard')
score = tbl.copy()
score.insert(0, 'Rank', score[f'NPV ({currency}/kWp)'].rank(ascending=False).astype(int)
             if rec['criterion'] == 'NPV' else
             (score[f'LCOE ({currency}/kWh)'].rank().astype(int) if rec['criterion'] == 'LCOE'
              else score['Net energy (kWh/kWp/yr)'].rank(ascending=False).astype(int)))
st.dataframe(fmt_table(score, 1))
with st.expander('How is the recommendation made?'):
    st.markdown(f"""
- The weather data are converted to the irradiance on each panel (fixed, single-axis, dual-axis) every hour, then to electrical energy with temperature, glass-reflection and system losses.
- Net energy subtracts the tracker's own motor energy. Costs, O&M, motor replacement, degradation and the discount rate give each option's **NPV** and **LCOE** over {lifetime} years.
- The **best option** follows the criterion you selected. To test robustness, 4,000 random scenarios vary the tariff, discount rate, degradation, tracker cost, O&M, motor energy use and the size of the tracker gain (model uncertainty). The share of scenarios in which each option wins is shown above.
- All cost inputs are **placeholders**. A result marked "close call" means the answer depends on them.
""")

# ----------------------------------------------------------------------------
# tabs
# ----------------------------------------------------------------------------
tabs = st.tabs(['Energy by year', 'Months and seasons', 'Why trackers help', 'Sensitivity',
                'Economics', 'Data and validation', 'Export'])

# ---- 1. Energy by year ----
with tabs[0]:
    a, b = st.columns(2)
    with a:
        m, s = R.E_year.mean()[CONFIGS], R.E_year.std()[CONFIGS].fillna(0)
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(CONFIGS, m, yerr=s, capsize=6, color=[COLORS[c] for c in CONFIGS])
        for i, c in enumerate(CONFIGS):
            ax.text(i, m[c] * 0.5, f'{m[c]:.0f}', ha='center', color='white', fontweight='bold')
        ax.set_ylabel('kWh per kWp per year'); ax.set_title('Mean annual energy (bars: ±1 std across years)')
        show(fig)
    with b:
        st.markdown('**Annual energy by year (kWh/kWp)**')
        st.line_chart(R.E_year[CONFIGS])
    st.markdown('**Summary across years**')
    st.dataframe(fmt_table(en.summary_table(R), 1))
    st.markdown('**Tracker gain over the fixed panel, by year (%)**')
    st.dataframe(fmt_table(R.gain_year[['Single-axis', 'Dual-axis']], 1))
    ranks_ok = bool(((R.E_year['Dual-axis'] > R.E_year['Single-axis']) & (R.E_year['Single-axis'] > R.E_year['Fixed'])).all())
    st.caption(f'Ranking Dual > Single > Fixed holds in every simulated year: **{ranks_ok}**. '
               'P90 is the yield exceeded in 90 % of years (with few years it is a rough estimate).')

# ---- 2. Months and seasons ----
with tabs[1]:
    mm = en.monthly_energy(R)
    mean_m, std_m = mm.groupby('Month').mean(), mm.groupby('Month').std().fillna(0)
    fig, ax = plt.subplots(figsize=(11, 4.5))
    w = 0.27
    for i, c in enumerate(CONFIGS):
        ax.bar(mean_m.index + (i - 1) * w, mean_m[c], w, yerr=std_m[c], capsize=2, label=c, color=COLORS[c])
    ax.set_xticks(range(1, 13)); ax.set_xlabel('Month'); ax.set_ylabel('kWh/kWp'); ax.legend()
    ax.set_title('Monthly energy (mean ± std across years)')
    show(fig)
    mg = en.monthly_gain(R)
    fig, ax = plt.subplots(figsize=(11, 4))
    for c in ['Single-axis', 'Dual-axis']:
        g = mg[c].groupby('Month')
        ax.plot(range(1, 13), g.mean(), marker='o', color=COLORS[c], label=c)
        ax.fill_between(range(1, 13), g.min(), g.max(), color=COLORS[c], alpha=0.15)
    ax.set_xticks(range(1, 13)); ax.set_xlabel('Month'); ax.set_ylabel('Gain over fixed (%)'); ax.legend()
    ax.set_title('Tracker gain by month (line: mean, band: min-max across years)')
    show(fig)
    if len(years) > 1:
        hm = mg['Single-axis'].unstack('Month')
        fig, ax = plt.subplots(figsize=(11, max(3, 0.4 * len(hm) + 1.5)))
        im = ax.imshow(hm.values, aspect='auto', cmap='YlGnBu'); ax.grid(False)
        ax.set_xticks(range(12)); ax.set_xticklabels(range(1, 13))
        ax.set_yticks(range(len(hm))); ax.set_yticklabels(hm.index)
        for i in range(hm.shape[0]):
            for j in range(hm.shape[1]):
                ax.text(j, i, f'{hm.values[i, j]:.0f}', ha='center', va='center', fontsize=7)
        plt.colorbar(im, label='%'); ax.set_title('Single-axis gain over fixed (%) by year and month')
        show(fig)

# ---- 3. Why trackers help ----
with tabs[2]:
    st.markdown('Trackers mostly help with **direct sunlight**. The more diffuse (cloudy) the sky, the less there is to gain.')
    kd = en.monthly_diffuse_fraction(W)
    mg = en.monthly_gain(R)
    a, b = st.columns(2)
    with a:
        if len(kd) >= 4:
            fig, ax = plt.subplots(figsize=(6, 4.5))
            for c in ['Single-axis', 'Dual-axis']:
                x, y = kd.values, mg[c].reindex(kd.index).values
                ok = ~(np.isnan(x) | np.isnan(y))
                ax.scatter(x[ok], y[ok], s=16, alpha=0.6, color=COLORS[c], label=c)
                if ok.sum() > 3:
                    k, b0 = np.polyfit(x[ok], y[ok], 1)
                    xs = np.linspace(x[ok].min(), x[ok].max(), 50)
                    ax.plot(xs, k * xs + b0, color=COLORS[c], label=f'{c} fit (R²={np.corrcoef(x[ok], y[ok])[0, 1] ** 2:.2f})')
            ax.set_xlabel('Monthly diffuse fraction (DHI/GHI)'); ax.set_ylabel('Gain over fixed (%)'); ax.legend()
            ax.set_title('Tracker gain falls as the sky gets more diffuse')
            show(fig)
    with b:
        sky = en.sky_table(sim, R)
        st.markdown('**Gain by sky condition (daily clearness index)**')
        st.dataframe(fmt_table(sky, 1))
        sky[['Single-axis gain (%)', 'Dual-axis gain (%)']].plot(kind='bar', rot=15, figsize=(6, 3.4),
                                                                  color=[COLORS['Single-axis'], COLORS['Dual-axis']])
        plt.axhline(0, color='k', lw=0.8); plt.ylabel('%'); plt.tight_layout()
        show(plt.gcf())
    sunny, cloudy = en.sunniest_cloudiest_days(sim)
    choice = st.radio('Daily profile', [f'Sunniest day ({sunny})', f'Cloudiest day ({cloudy})'], horizontal=True)
    day = sunny if choice.startswith('Sunniest') else cloudy
    prof = en.day_profile(sim, R, day)
    st.line_chart(prof[CONFIGS])
    st.caption('Irradiance on each panel (W/m²) against local solar time (hours).')

# ---- 4. Sensitivity ----
with tabs[3]:
    if R.sweep is not None:
        st.markdown('**Fixed-panel baseline: yield against tilt**')
        fig, ax = plt.subplots(figsize=(8, 3.8))
        for col in R.sweep.columns:
            sr = R.sweep[col].dropna(); ax.plot(sr.index, sr.values, marker='o', label=col)
        ax.axvline(R.fixed_tilt, color='k', ls='--', lw=1)
        ax.set_xlabel('Tilt (°)'); ax.set_ylabel('kWh/kWp/yr'); ax.legend()
        ax.set_title(f'Best fixed setting: tilt {R.fixed_tilt:.0f}°, azimuth {R.fixed_az:.0f}°')
        show(fig)
    if not sens:
        st.info('Sensitivity studies were switched off. Enable them in the sidebar (Panel and model settings) and run again.')
    else:
        a, b = st.columns(2)
        with a:
            st.markdown('**Single-axis rotation limit (kWh/kWp/yr)**')
            st.line_chart(sens['max_angle'])
        with b:
            st.markdown('**Effect of glass-reflection (IAM) loss**')
            st.dataframe(fmt_table(sens['iam'], 1))
        st.markdown('**Transposition-model uncertainty** (isotropic is known to under-estimate; use the others as the plausible range)')
        st.dataframe(fmt_table(sens['models'], 1))
        st.markdown('**Time-stamp alignment (sun position offset)**')
        st.dataframe(fmt_table(sens['shift'], 1))
        st.caption('The size of the tracker gain moves by about a percentage point or less with the alignment; the ranking is unaffected.')

# ---- 5. Economics ----
with tabs[4]:
    st.markdown(f'**Lifecycle results per kWp** ({lifetime} years, {discount * 100:.1f}% discount rate)')
    st.dataframe(fmt_table(tbl.T, 1))
    cc = pd.DataFrame({c: v for c, v in cum_curves.items()}, index=np.arange(1, lifetime + 1))
    st.markdown(f'**Cumulative discounted extra cash flow versus fixed ({currency}/kWp)**: crossing zero is the discounted payback.')
    st.line_chart(cc)

    tariffs, x1, x2, Z1, Z2 = en.sensitivity_grids(net, ec)
    st.markdown('**Where does each option win?** Blue: the more complex option has the higher NPV. Black line: break-even.')
    fig, axes = plt.subplots(1, 2, figsize=(14, 4.8))
    for ax, Z, xs, xl, tt in [(axes[0], Z1, x1, f'Extra CAPEX of single-axis ({currency}/kWp)', 'Single-axis vs Fixed'),
                              (axes[1], Z2, x2, f'Extra CAPEX of second axis ({currency}/kWp)', 'Dual-axis vs Single-axis')]:
        lim = max(np.abs(Z).max(), 1e-9)
        im = ax.pcolormesh(xs, tariffs, Z, cmap='RdBu', vmin=-lim, vmax=lim, shading='auto')
        if Z.min() < 0 < Z.max():
            ax.contour(xs, tariffs, Z, levels=[0], colors='k', linewidths=2)
        ax.grid(False); ax.set_xlabel(xl); ax.set_ylabel(f'Tariff ({currency}/kWh)'); ax.set_title(tt + ': NPV difference')
        plt.colorbar(im, ax=ax, label=f'{currency}/kWp')
    plt.tight_layout(); show(fig)

    st.markdown('**Monte Carlo: spread of lifetime NPV across 4,000 random scenarios**')
    fig, ax = plt.subplots(figsize=(10, 3.8))
    for c in CONFIGS:
        ax.hist(mc_npv[c], bins=50, alpha=0.5, label=c, color=COLORS[c])
    ax.set_xlabel(f'NPV ({currency}/kWp)'); ax.set_ylabel('Scenarios'); ax.legend()
    show(fig)
    st.caption(f"Tracker-gain uncertainty used: {res['gain_range'][0]:.2f}x to {res['gain_range'][1]:.2f}x of the base gain "
               '(from the transposition-model comparison).')

# ---- 6. Data and validation ----
with tabs[5]:
    st.markdown('**Data coverage by year**')
    st.dataframe(cov)
    if not W.attrs.get('has_wind', False):
        st.caption('No wind-speed data in the file: a constant wind speed is assumed for the cell temperature.')
    st.markdown('**Average daily irradiance profile (local solar time)**')
    st.line_chart(en.mean_daily_profile(W, res['lon']))
    if 'closure' in sens:
        cl = sens['closure']
        best_shift = int(cl.idxmin())
        st.markdown('**Time-stamp alignment check**')
        st.caption('For each candidate offset, the plot shows how well GHI = DNI × cos(zenith) + DHI closes. '
                   'The minimum indicates where the hourly stamp sits relative to the sun. '
                   f'Minimum at **{best_shift:+d} min**; the model uses {sim.st.shift_min:+d} min.')
        st.line_chart(cl)
    st.divider()
    st.markdown('**Validate against PVGIS** (https://re.jrc.ec.europa.eu/pvg_tools/). Run PVGIS with the same location, '
                '1 kWp and the same system loss, then enter the yearly kWh/kWp. Leave a value at 0 to skip it.')
    c1, c2, c3 = st.columns(3)
    pv_tilt = c1.number_input('PVGIS fixed tilt (°)', 0.0, 90.0, float(R.fixed_tilt), 1.0)
    pv_az = c2.number_input('PVGIS fixed azimuth (° from north)', 0.0, 360.0, float(R.fixed_az), 1.0)
    c1, c2, c3 = st.columns(3)
    pv_f = c1.number_input('PVGIS fixed (kWh/kWp/yr)', 0.0, 4000.0, 0.0, 1.0)
    pv_s = c2.number_input('PVGIS single-axis (kWh/kWp/yr)', 0.0, 4000.0, 0.0, 1.0)
    pv_d = c3.number_input('PVGIS dual-axis (kWh/kWp/yr)', 0.0, 4000.0, 0.0, 1.0)
    if any(v > 0 for v in (pv_f, pv_s, pv_d)):
        model_fixed = sim.mean_annual(sim.orient_fixed(pv_tilt, pv_az))
        rows = {}
        for name, pv, mod in [('Fixed', pv_f, model_fixed), ('Single-axis', pv_s, E_mean['Single-axis']),
                              ('Dual-axis', pv_d, E_mean['Dual-axis'])]:
            if pv > 0:
                rows[name] = {'PVGIS (kWh/kWp)': pv, 'This app (kWh/kWp)': mod, 'Difference (%)': (mod / pv - 1) * 100}
        v = pd.DataFrame(rows).T
        if pv_f > 0:
            for name in v.index:
                if name != 'Fixed' and name in rows:
                    v.loc[name, 'PVGIS gain vs fixed (%)'] = (rows[name]['PVGIS (kWh/kWp)'] / pv_f - 1) * 100
                    v.loc[name, 'App gain vs fixed (%)'] = (rows[name]['This app (kWh/kWp)'] / model_fixed - 1) * 100
        st.dataframe(fmt_table(v, 1))
        st.caption('PVGIS uses a different irradiance database, so a few percent difference is normal. '
                   'The tracker gain over fixed is the number the recommendation relies on, so compare that first.')

# ---- 7. Export ----
with tabs[6]:
    summary = en.summary_table(R)
    tables = {'Summary': summary, 'Yearly energy': R.E_year, 'Yearly gain': R.gain_year,
              'Monthly mean energy': en.monthly_energy(R).groupby('Month').mean(),
              'Economics': tbl.T, 'Scenario win share': (rec['probs'].to_frame('Share') if rec['probs'] is not None else pd.DataFrame()),
              'Data coverage': cov}
    for k, v in sens.items():
        if isinstance(v, (pd.DataFrame, pd.Series)):
            tables['Sens ' + k] = v.to_frame() if isinstance(v, pd.Series) else v
    st.download_button('⬇ Download all results (Excel)', make_excel(tables), 'tracker_selector_results.xlsx',
                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    report = [f'# Solar tracker selection report', '',
              f"Location: {res['lat']:.4f}, {res['lon']:.4f}", f"Data: {res['source_text']}",
              f"Years simulated: {years[0]}-{years[-1]} ({len(years)} years)",
              f'Fixed baseline: tilt {R.fixed_tilt:.0f} deg, azimuth {R.fixed_az:.0f} deg', '',
              f'## Recommendation ({crit_label}): {best}', ''] + ['- ' + l.replace('**', '') for l in en.explanation(best, rec, tbl, R, ec)] + \
             ['', '## Energy summary', '```', summary.round(1).to_string(), '```',
              '', '## Economics (per kWp; inputs are user assumptions)', '```', tbl.T.round(1).to_string(), '```']
    st.download_button('⬇ Download summary report (Markdown)', '\n'.join(report), 'tracker_selector_report.md', 'text/markdown')
    st.dataframe(fmt_table(summary, 1))
