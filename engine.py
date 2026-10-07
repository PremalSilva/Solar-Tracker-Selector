"""
engine.py - simulation engine for the Solar Tracker Selector web app.

Everything the notebook does, packaged as functions:
  * NASA POWER download (by latitude/longitude) or CSV parsing
  * data cleaning and quality checks
  * fixed / single-axis / dual-axis plane-of-array and energy simulation (pvlib)
  * statistics across years, sky-condition analysis, sensitivity studies
  * lifecycle economics (NPV, LCOE, payback), Monte Carlo, recommendation
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pvlib
import requests

CONFIGS = ['Fixed', 'Single-axis', 'Dual-axis']

NASA_URL = 'https://power.larc.nasa.gov/api/temporal/hourly/point'
NASA_PARAMS = 'ALLSKY_SFC_SW_DWN,ALLSKY_SFC_SW_DNI,ALLSKY_SFC_SW_DIFF,T2M,WS10M'
RENAME = {'DNI': 'dni', 'ALLSKY_SFC_SW_DNI': 'dni',
          'DHI': 'dhi', 'ALLSKY_SFC_SW_DIFF': 'dhi',
          'GHI': 'ghi', 'ALLSKY_SFC_SW_DWN': 'ghi',
          'Tair': 'temp_air', 'T2M': 'temp_air',
          'WS10M': 'wind_speed', 'WS2M': 'wind_speed', 'WS': 'wind_speed'}


class DataError(Exception):
    """Raised for any problem with the weather data."""


# ----------------------------------------------------------------------------
# 1. DATA ACCESS
# ----------------------------------------------------------------------------
def _parse_nasa_json(js: dict):
    try:
        par = js['properties']['parameter']
    except KeyError as e:
        raise DataError(f'Unexpected NASA POWER response (missing {e}).')
    d = pd.DataFrame(par)
    d.index = pd.to_datetime(d.index.astype(str), format='%Y%m%d%H')
    d = d.sort_index().rename(columns=RENAME)
    d = d.apply(pd.to_numeric, errors='coerce').mask(lambda x: x <= -990)
    elev = None
    try:
        elev = js['geometry']['coordinates'][2]
    except Exception:
        pass
    return d, elev


def fetch_nasa_year(lat, lon, year, retries=3, timeout=180):
    """Download one calendar year of hourly data (UTC time standard)."""
    params = {'parameters': NASA_PARAMS, 'community': 'RE',
              'longitude': lon, 'latitude': lat,
              'start': f'{year}0101', 'end': f'{year}1231',
              'format': 'JSON', 'time-standard': 'UTC'}
    last = ''
    for k in range(retries):
        try:
            r = requests.get(NASA_URL, params=params, timeout=timeout)
            if r.status_code == 200:
                return _parse_nasa_json(r.json())
            last = f'HTTP {r.status_code}: {r.text[:200]}'
            if r.status_code in (400, 422):      # bad request: retrying will not help
                break
        except (requests.RequestException, ValueError) as e:
            last = str(e)
        time.sleep(2 * (k + 1))
    raise DataError(f'NASA POWER download failed for {year}. {last}')


def fetch_nasa(lat, lon, y0, y1, progress=None):
    """Download y0..y1 (inclusive). Returns (raw UTC DataFrame, elevation in m)."""
    frames, elev = [], None
    years = list(range(int(y0), int(y1) + 1))
    for i, y in enumerate(years):
        if progress:
            progress(i, len(years), y)
        d, e = fetch_nasa_year(lat, lon, y)
        frames.append(d)
        elev = e if elev is None else elev
    if progress:
        progress(len(years), len(years), None)
    raw = pd.concat(frames)
    raw.index = raw.index.tz_localize('UTC')
    return raw, elev


def read_power_csv_text(text: str) -> pd.DataFrame:
    """Parse a NASA POWER CSV (single 't' column, or YEAR/MO/DY/HR layout)."""
    lines = text.splitlines()
    skip = 0
    for i, line in enumerate(lines[:100]):
        if line.strip().startswith('-END HEADER-'):
            skip = i + 1
            break
    d = pd.read_csv(io.StringIO('\n'.join(lines[skip:])))
    d.columns = [c.strip() for c in d.columns]
    if 't' in d.columns:
        try:
            idx = pd.to_datetime(d['t'], format='%d-%b-%Y %H:%M:%S')
        except Exception:
            idx = pd.to_datetime(d['t'])
    elif {'YEAR', 'MO', 'DY', 'HR'} <= set(d.columns):
        idx = pd.to_datetime(dict(year=d['YEAR'], month=d['MO'], day=d['DY'], hour=d['HR']))
    else:
        raise DataError('Cannot find a time column (expected "t" or YEAR/MO/DY/HR). '
                        f'Columns found: {list(d.columns)}')
    d.index = idx
    d = d.rename(columns=RENAME)
    keep = [c for c in ['ghi', 'dni', 'dhi', 'temp_air', 'wind_speed'] if c in d.columns]
    d = d[keep].apply(pd.to_numeric, errors='coerce')
    return d.mask(d <= -990)


def to_utc(df: pd.DataFrame, mode: str, lon: float, offset_hours: float = 0.0) -> pd.DataFrame:
    """Convert a naive-time DataFrame to UTC.
    mode: 'UTC' | 'LST' (NASA local solar time = UTC + round(lon/15)) | 'CLOCK' (UTC + offset_hours).
    A fractional offset (e.g. +5.5 h) leaves a residual of a few minutes after flooring to the hour;
    it is stored in attrs['frac_shift_min'] and must be added to the mid-hour sun-position shift."""
    off = {'UTC': 0.0, 'LST': float(round(lon / 15.0))}.get(mode, float(offset_hours))
    t = df.index - pd.Timedelta(hours=off)
    floored = t.floor('h')
    out = df.copy()
    out.index = floored.tz_localize('UTC')
    frac = float(((t - floored).total_seconds() / 60.0).to_numpy()[0]) if len(t) else 0.0
    out.attrs['frac_shift_min'] = frac
    return out


def prepare_weather(raw_utc: pd.DataFrame, default_wind=1.0, min_cov=0.95):
    """Complete the hourly axis, fill short gaps, report coverage per year.
    Returns (W, coverage table, years used)."""
    missing = {'ghi', 'dni', 'dhi', 'temp_air'} - set(raw_utc.columns)
    if missing:
        raise DataError(f'Required columns missing: {sorted(missing)}')
    raw = raw_utc[~raw_utc.index.duplicated(keep='first')].sort_index()
    if raw.empty:
        raise DataError('The weather data is empty.')
    start = pd.Timestamp(year=raw.index.min().year, month=1, day=1, tz='UTC')
    end = pd.Timestamp(year=raw.index.max().year, month=12, day=31, hour=23, tz='UTC')
    W = raw.reindex(pd.date_range(start, end, freq='h'))
    miss = W[['ghi', 'dni', 'dhi']].isna().any(axis=1)

    W = W.interpolate(limit=3, limit_area='inside')
    W[['ghi', 'dni', 'dhi']] = W[['ghi', 'dni', 'dhi']].fillna(0).clip(lower=0)
    W['temp_air'] = W['temp_air'].ffill().bfill()
    W['wind_speed'] = W['wind_speed'].fillna(default_wind) if 'wind_speed' in W.columns else default_wind
    has_wind = 'wind_speed' in raw.columns

    rows = []
    for y in sorted(set(W.index.year)):
        n_exp = 8784 if pd.Timestamp(year=y, month=1, day=1).is_leap_year else 8760
        sel = W.index.year == y
        n_miss = int(miss[sel].sum())
        rows.append({'Year': y, 'Expected hours': n_exp, 'Missing hours': n_miss,
                     'Coverage': round(1 - n_miss / n_exp, 4)})
    cov = pd.DataFrame(rows).set_index('Year')
    cov['Used'] = cov['Coverage'] >= min_cov
    years = [y for y in cov.index if cov.loc[y, 'Used']]
    if not years:
        raise DataError('No year has enough valid data (coverage below threshold).')
    W = W[W.index.year.isin(years)]
    W.attrs['has_wind'] = has_wind
    W.attrs['frac_shift_min'] = float(raw_utc.attrs.get('frac_shift_min', 0.0))
    return W, cov, years


def to_local_solar(obj, lon):
    """Shift a UTC-indexed object to approximate local solar time (naive index), for plots and daily stats."""
    out = obj.copy()
    out.index = out.index.tz_localize(None) + pd.Timedelta(hours=lon / 15.0)
    return out


# ----------------------------------------------------------------------------
# 2. SIMULATION
# ----------------------------------------------------------------------------
@dataclass
class Settings:
    fixed_tilt: float | None = None      # None = optimise
    fixed_az: float | None = None
    max_angle: float = 60.0
    albedo: float = 0.2
    transposition: str = 'haydavies'
    gamma: float = -0.004
    system_losses: float = 0.86
    use_iam: bool = True
    shift_min: int = 30                  # evaluate the sun at mid-hour (hour-beginning labels)
    pdc0: float = 1000.0                 # W -> results per kWp


class Sim:
    def __init__(self, W, lat, lon, alt, st: Settings):
        self.W, self.lat, self.lon, self.alt, self.st = W, lat, lon, alt or 0.0, st
        self.years = sorted(set(W.index.year))
        self.eq_az = 180.0 if lat >= 0 else 0.0           # equator-facing azimuth
        self.dni_extra = pvlib.irradiance.get_extra_radiation(W.index)
        self._sp = {}

    # -- geometry ------------------------------------------------------------
    def solpos(self, shift=None):
        shift = self.st.shift_min if shift is None else shift
        if shift not in self._sp:
            sp = pvlib.solarposition.get_solarposition(
                self.W.index + pd.Timedelta(minutes=shift), self.lat, self.lon, altitude=self.alt)
            sp.index = self.W.index
            self._sp[shift] = sp
        return self._sp[shift]

    def orient_fixed(self, tilt, az=None):
        return pd.DataFrame({'surface_tilt': float(tilt),
                             'surface_azimuth': self.eq_az if az is None else float(az)}, index=self.W.index)

    def orient_single(self, max_angle=None, shift=None):
        sp = self.solpos(shift)
        s = pvlib.tracking.singleaxis(sp['apparent_zenith'], sp['azimuth'], axis_tilt=0,
                                      axis_azimuth=180, max_angle=self.st.max_angle if max_angle is None else max_angle,
                                      backtrack=False)
        return s.fillna(0)

    def orient_dual(self, shift=None):
        sp = self.solpos(shift)
        return pd.DataFrame({'surface_tilt': sp['apparent_zenith'].clip(0, 90),
                             'surface_azimuth': sp['azimuth']}, index=self.W.index)

    # -- irradiance and energy -----------------------------------------------
    def poa(self, orient, shift=None, model=None, use_iam=None):
        st, W, sp = self.st, self.W, self.solpos(shift)
        model = model or st.transposition
        use_iam = st.use_iam if use_iam is None else use_iam
        p = pvlib.irradiance.get_total_irradiance(
            orient['surface_tilt'], orient['surface_azimuth'], sp['apparent_zenith'], sp['azimuth'],
            dni=W['dni'], ghi=W['ghi'], dhi=W['dhi'], dni_extra=self.dni_extra,
            albedo=st.albedo, model=model)
        direct = p['poa_direct'].fillna(0).clip(lower=0)
        diffuse = (p['poa_sky_diffuse'].fillna(0) + p['poa_ground_diffuse'].fillna(0)).clip(lower=0)
        glob = direct + diffuse
        if use_iam:
            aoi = pvlib.irradiance.aoi(orient['surface_tilt'], orient['surface_azimuth'],
                                       sp['apparent_zenith'], sp['azimuth'])
            direct = direct * pvlib.iam.physical(aoi).fillna(0).clip(0, 1)
        return pd.DataFrame({'poa_global': glob, 'poa_eff': direct + diffuse})

    def energy(self, poa):
        st, W = self.st, self.W
        tc = pvlib.temperature.faiman(poa['poa_global'], W['temp_air'], wind_speed=W['wind_speed'])
        p = pvlib.pvsystem.pvwatts_dc(poa['poa_eff'], tc, st.pdc0, st.gamma)
        return (p * st.system_losses / 1000).fillna(0)          # kWh per kWp per hour

    def yearly_energy(self, orient, **kw):
        e = self.energy(self.poa(orient, **kw))
        return e.groupby(e.index.year).sum()

    def mean_annual(self, orient, **kw):
        return float(self.yearly_energy(orient, **kw).mean())

    # -- fixed-tilt optimisation ---------------------------------------------
    def optimise_fixed(self):
        azs = [self.eq_az] + ([(self.eq_az + 180) % 360] if abs(self.lat) < 12 else [])
        rows = {}
        for az in azs:
            for t in range(0, 61, 5):
                rows[(az, t)] = self.mean_annual(self.orient_fixed(t, az))
        az, t0 = max(rows, key=rows.get)
        for t in range(max(0, t0 - 4), min(90, t0 + 4) + 1):
            rows.setdefault((az, t), self.mean_annual(self.orient_fixed(t, az)))
        az, tilt = max(rows, key=rows.get)
        sweep = pd.DataFrame({('Equator-facing' if a == self.eq_az else 'Pole-facing'):
                              pd.Series({t: v for (aa, t), v in rows.items() if aa == a}).sort_index()
                              for a in azs})
        return float(tilt), float(az), sweep

    # -- main run --------------------------------------------------------------
    def run(self):
        st = self.st
        if st.fixed_tilt is None:
            tilt, az, sweep = self.optimise_fixed()
        else:
            tilt, az, sweep = st.fixed_tilt, (self.eq_az if st.fixed_az is None else st.fixed_az), None
        self._fixed_tilt, self._fixed_az = tilt, az
        orient = {'Fixed': self.orient_fixed(tilt, az), 'Single-axis': self.orient_single(),
                  'Dual-axis': self.orient_dual()}
        poa = {k: self.poa(v) for k, v in orient.items()}
        R = Results()
        R.orient, R.fixed_tilt, R.fixed_az, R.sweep = orient, tilt, az, sweep
        R.E_hourly = pd.DataFrame({k: self.energy(poa[k]) for k in CONFIGS})
        R.POA = pd.DataFrame({k: poa[k]['poa_global'] for k in CONFIGS})
        R.E_year, R.I_year, R.gain_year = yearly_tables(R)
        return R


class Results:
    pass


# ----------------------------------------------------------------------------
# 3. STATISTICS
# ----------------------------------------------------------------------------
def yearly_tables(R):
    E = R.E_hourly
    E_year = E.groupby(E.index.year).sum()
    I_year = R.POA.groupby(R.POA.index.year).sum() / 1000
    gain = pd.DataFrame({k: (E_year[k] / E_year['Fixed'] - 1) * 100 for k in CONFIGS})
    return E_year, I_year, gain


def summary_table(R):
    E, G = R.E_year, R.gain_year
    out = pd.DataFrame({
        'Mean energy (kWh/kWp/yr)': E.mean(),
        'Std dev': E.std(),
        'P50': E.quantile(0.5),
        'P90 (exceeded in 90% of years)': E.quantile(0.10),
        'Capacity factor (%)': E.mean() / (8766 * 1.0) * 100,
        'Mean gain vs fixed (%)': G.mean(),
        'Min gain (%)': G.min(),
        'Max gain (%)': G.max()})
    return out.loc[CONFIGS]


def monthly_energy(R):
    E = R.E_hourly
    m = E.groupby([E.index.year, E.index.month]).sum()
    m.index.names = ['Year', 'Month']
    return m


def monthly_gain(R):
    m = monthly_energy(R)
    return pd.DataFrame({c: (m[c] / m['Fixed'] - 1) * 100 for c in ['Single-axis', 'Dual-axis']})


def monthly_diffuse_fraction(W):
    g = W.groupby([W.index.year, W.index.month])
    kd = g['dhi'].sum() / g['ghi'].sum().replace(0, np.nan)
    kd.index.names = ['Year', 'Month']
    return kd


def sky_table(sim, R):
    sp = sim.solpos()
    ghi_extra_h = (sim.dni_extra * np.cos(np.radians(sp['zenith']))).clip(lower=0)
    ghi_l = to_local_solar(sim.W['ghi'], sim.lon)
    ext_l = to_local_solar(ghi_extra_h, sim.lon)
    kt = (ghi_l.resample('D').sum() / ext_l.resample('D').sum()).replace([np.inf, -np.inf], np.nan).dropna()
    E_day = to_local_solar(R.E_hourly, sim.lon).resample('D').sum().reindex(kt.index)
    labels = ['Overcast (<0.30)', 'Cloudy (0.30-0.50)', 'Partly clear (0.50-0.65)', 'Clear (>0.65)']
    cls = pd.cut(kt, [0, 0.3, 0.5, 0.65, 1.2], labels=labels)
    byc = E_day.groupby(cls, observed=False).sum()
    out = pd.DataFrame({
        'Share of days (%)': (cls.value_counts(normalize=True) * 100).reindex(labels),
        'Single-axis gain (%)': (byc['Single-axis'] / byc['Fixed'] - 1) * 100,
        'Dual-axis gain (%)': (byc['Dual-axis'] / byc['Fixed'] - 1) * 100})
    return out


def mean_daily_profile(W, lon):
    L = to_local_solar(W[['ghi', 'dni', 'dhi']], lon)
    return L.groupby(L.index.hour).mean()


def day_profile(sim, R, day):
    P = to_local_solar(R.POA, sim.lon)
    d = P.loc[str(day)]
    return d.set_index(d.index.hour + d.index.minute / 60)


def sunniest_cloudiest_days(sim):
    g = to_local_solar(sim.W['ghi'], sim.lon).resample('D').sum()
    g = g[g > 0]
    return g.idxmax().date(), g.idxmin().date()


# ----------------------------------------------------------------------------
# 4. SENSITIVITY STUDIES
# ----------------------------------------------------------------------------
def _gains(e):
    return {'Single-axis gain (%)': (e['Single-axis'] / e['Fixed'] - 1) * 100,
            'Dual-axis gain (%)': (e['Dual-axis'] / e['Fixed'] - 1) * 100}


def sens_max_angle(sim, angles=(30, 45, 60, 75, 90)):
    return pd.Series({a: sim.mean_annual(sim.orient_single(a)) for a in angles}, name='Mean annual energy (kWh/kWp)')


def sens_models(sim, R, models=('isotropic', 'haydavies', 'klucher', 'reindl')):
    rows = {}
    for m in models:
        e = {c: sim.mean_annual(R.orient[c], model=m) for c in CONFIGS}
        rows[m] = {**e, **_gains(e)}
    return pd.DataFrame(rows).T


def sens_iam(sim, R):
    rows = {}
    for lab, flag in [('IAM off', False), ('IAM on', True)]:
        e = {c: sim.mean_annual(R.orient[c], use_iam=flag) for c in CONFIGS}
        rows[lab] = {**e, **_gains(e)}
    return pd.DataFrame(rows).T


def sens_shift(sim, shifts=None):
    base = sim.st.shift_min
    shifts = shifts or [base - 30, base, base + 30]
    rows = {}
    for s in shifts:
        o = {'Fixed': sim.orient_fixed(sim._fixed_tilt, sim._fixed_az),
             'Single-axis': sim.orient_single(shift=s), 'Dual-axis': sim.orient_dual(shift=s)}
        e = {c: sim.mean_annual(o[c], shift=s) for c in CONFIGS}
        rows[f'{s:+d} min' + (' (used)' if s == base else '')] = {**e, **_gains(e)}
    return pd.DataFrame(rows).T


def closure_test(W, lat, lon, alt, shifts=range(-60, 151, 15), max_years=3):
    """Timestamp-alignment diagnostic. For each candidate shift, evaluates how well
    GHI = DNI*cos(zenith) + DHI closes when the sun position is computed at (stamp + shift).
    The shift with the smallest RMSE is the best-supported alignment (minutes after the stamp)."""
    yrs = sorted(set(W.index.year))[:max_years]
    d = W[W.index.year.isin(yrs)]
    rows = {}
    for s in shifts:
        sp = pvlib.solarposition.get_solarposition(d.index + pd.Timedelta(minutes=s), lat, lon, altitude=alt)
        sp.index = d.index
        cz = np.cos(np.radians(sp['apparent_zenith']))
        m = (sp['apparent_zenith'] < 75) & (d['ghi'] > 20)
        clos = d['ghi'] - (d['dni'] * cz + d['dhi'])
        rows[s] = float(np.sqrt((clos[m] ** 2).mean()))
    return pd.Series(rows, name='Closure RMSE (W/m2)')


# ----------------------------------------------------------------------------
# 5. ECONOMICS
# ----------------------------------------------------------------------------
@dataclass
class Econ:
    currency: str = 'LKR'
    lifetime: int = 25
    discount: float = 0.10
    degradation: float = 0.005
    tariff: float = 30.0
    tariff_esc: float = 0.0
    om_esc: float = 0.03
    repl_year: int = 10
    capex: dict = field(default_factory=lambda: {'Fixed': 200000., 'Single-axis': 240000., 'Dual-axis': 290000.})
    om: dict = field(default_factory=lambda: {'Fixed': 1500., 'Single-axis': 2500., 'Dual-axis': 4000.})
    repl: dict = field(default_factory=lambda: {'Fixed': 0., 'Single-axis': 8000., 'Dual-axis': 16000.})
    tracker_kwh: dict = field(default_factory=lambda: {'Fixed': 0., 'Single-axis': 15., 'Dual-axis': 30.})


def lifecycle(e1, capex, om, repl, ec: Econ, tariff=None):
    yrs = np.arange(1, ec.lifetime + 1)
    tariff = ec.tariff if tariff is None else tariff
    e_t = e1 * (1 - ec.degradation) ** (yrs - 1)
    revenue = e_t * tariff * (1 + ec.tariff_esc) ** (yrs - 1)
    cost = om * (1 + ec.om_esc) ** (yrs - 1) + np.where(yrs == ec.repl_year, repl, 0.0)
    disc = (1 + ec.discount) ** (-yrs.astype(float))
    cash = revenue - cost
    npv = -capex + (cash * disc).sum()
    lcoe = (capex + (cost * disc).sum()) / (e_t * disc).sum()
    return npv, lcoe, cash, disc


def econ_table(E_mean, ec: Econ):
    net = pd.Series({c: E_mean[c] - ec.tracker_kwh[c] for c in CONFIGS})
    rows, cash_f = {}, {}
    for c in CONFIGS:
        npv, lcoe, cash, disc = lifecycle(net[c], ec.capex[c], ec.om[c], ec.repl[c], ec)
        rows[c] = {'Gross energy (kWh/kWp/yr)': E_mean[c], 'Net energy (kWh/kWp/yr)': net[c],
                   'Net gain vs fixed (%)': (net[c] / net['Fixed'] - 1) * 100,
                   f'CAPEX ({ec.currency}/kWp)': ec.capex[c],
                   f'NPV ({ec.currency}/kWp)': npv, f'LCOE ({ec.currency}/kWh)': lcoe}
        cash_f[c] = cash
    t = pd.DataFrame(rows).T
    t[f'NPV vs fixed ({ec.currency}/kWp)'] = t[f'NPV ({ec.currency}/kWp)'] - t.loc['Fixed', f'NPV ({ec.currency}/kWp)']
    pb_s, pb_d = {}, {}
    cum_curves = {}
    for c in ['Single-axis', 'Dual-axis']:
        inc = cash_f[c] - cash_f['Fixed']
        extra = ec.capex[c] - ec.capex['Fixed']
        cum_d = -extra + np.cumsum(inc * disc)
        cum_curves[c] = cum_d
        pb_s[c] = extra / inc[0] if inc[0] > 0 else np.nan
        pb_d[c] = float(np.argmax(cum_d >= 0) + 1) if (cum_d >= 0).any() else np.nan
    t['Simple payback vs fixed (yr)'] = pd.Series(pb_s)
    t['Discounted payback vs fixed (yr)'] = pd.Series(pb_d)
    return t, net, cum_curves


def sensitivity_grids(net, ec: Econ, n=31):
    tariffs = np.linspace(ec.tariff * 0.5, ec.tariff * 2.0, n)
    extra_single = np.linspace(max(1.0, 0.1 * (ec.capex['Single-axis'] - ec.capex['Fixed'])),
                               3.0 * max(1.0, ec.capex['Single-axis'] - ec.capex['Fixed']), n)
    extra_second = np.linspace(0, 3.0 * max(1.0, ec.capex['Dual-axis'] - ec.capex['Single-axis']), n)
    Z1 = np.zeros((n, n)); Z2 = np.zeros((n, n))
    for i, t in enumerate(tariffs):
        nf = lifecycle(net['Fixed'], ec.capex['Fixed'], ec.om['Fixed'], ec.repl['Fixed'], ec, t)[0]
        ns = lifecycle(net['Single-axis'], ec.capex['Single-axis'], ec.om['Single-axis'], ec.repl['Single-axis'], ec, t)[0]
        for j, x in enumerate(extra_single):
            Z1[i, j] = lifecycle(net['Single-axis'], ec.capex['Fixed'] + x, ec.om['Single-axis'],
                                 ec.repl['Single-axis'], ec, t)[0] - nf
        for j, x in enumerate(extra_second):
            Z2[i, j] = lifecycle(net['Dual-axis'], ec.capex['Single-axis'] + x, ec.om['Dual-axis'],
                                 ec.repl['Dual-axis'], ec, t)[0] - ns
    return tariffs, extra_single, extra_second, Z1, Z2


def _life_vec(e1, capex, om, repl, tariff, r, deg, ec: Econ):
    yrs = np.arange(1, ec.lifetime + 1)[None, :].astype(float)
    col = lambda a: np.asarray(a, float)[:, None]
    e_t = col(e1) * (1 - col(deg)) ** (yrs - 1)
    rev = e_t * col(tariff) * (1 + ec.tariff_esc) ** (yrs - 1)
    cost = col(om) * (1 + ec.om_esc) ** (yrs - 1) + np.where(yrs == ec.repl_year, col(repl), 0.0)
    disc = (1 + col(r)) ** (-yrs)
    npv = -np.asarray(capex, float) + ((rev - cost) * disc).sum(1)
    lcoe = (np.asarray(capex, float) + (cost * disc).sum(1)) / (e_t * disc).sum(1)
    return npv, lcoe


def monte_carlo(E_mean, ec: Econ, gain_range=(0.85, 1.05), n=4000, seed=1):
    """Random scenarios for tariff, discount rate, degradation, tracker cost, O&M, tracker
    consumption and the size of the tracker energy gain (model uncertainty)."""
    rng = np.random.default_rng(seed)
    tar = ec.tariff * rng.uniform(0.7, 1.3, n)
    r = np.clip(ec.discount + rng.uniform(-0.03, 0.03, n), 0.01, None)
    deg = rng.uniform(0.003, 0.008, n)
    cap_m, om_m, cons_m = rng.uniform(0.75, 1.25, n), rng.uniform(0.5, 1.5, n), rng.uniform(0.5, 1.5, n)
    gf = rng.uniform(*gain_range, n)
    npv, lcoe = {}, {}
    for c in CONFIGS:
        if c == 'Fixed':
            e = np.full(n, E_mean['Fixed']); capex = np.full(n, ec.capex['Fixed'])
            om = np.full(n, ec.om['Fixed']); repl = np.full(n, ec.repl['Fixed'])
        else:
            e = E_mean['Fixed'] + gf * (E_mean[c] - E_mean['Fixed']) - ec.tracker_kwh[c] * cons_m
            capex = ec.capex['Fixed'] + (ec.capex[c] - ec.capex['Fixed']) * cap_m
            om = ec.om['Fixed'] + (ec.om[c] - ec.om['Fixed']) * om_m
            repl = ec.repl[c] * om_m
        npv[c], lcoe[c] = _life_vec(e, capex, om, repl, tar, r, deg, ec)
    return pd.DataFrame(npv), pd.DataFrame(lcoe)


CRITERIA = {'Maximum lifetime value (NPV)': 'NPV',
            'Lowest cost of energy (LCOE)': 'LCOE',
            'Maximum energy yield': 'Energy'}


def recommend(tbl, E_year, mc_npv, mc_lcoe, ec: Econ, criterion='NPV'):
    cur = ec.currency
    npv_col, lcoe_col = f'NPV ({cur}/kWp)', f'LCOE ({cur}/kWh)'
    if criterion == 'NPV':
        best = tbl[npv_col].idxmax(); order = tbl[npv_col].sort_values(ascending=False)
        probs = mc_npv.idxmax(axis=1).value_counts(normalize=True).reindex(CONFIGS).fillna(0)
    elif criterion == 'LCOE':
        best = tbl[lcoe_col].idxmin(); order = tbl[lcoe_col].sort_values()
        probs = mc_lcoe.idxmin(axis=1).value_counts(normalize=True).reindex(CONFIGS).fillna(0)
    else:
        best = tbl['Net energy (kWh/kWp/yr)'].idxmax(); order = tbl['Net energy (kWh/kWp/yr)'].sort_values(ascending=False)
        probs = None
    notes = []
    if tbl[npv_col].max() < 0:
        notes.append('No option has a positive NPV with the current cost inputs, so none pays for itself. '
                     'Check the economic assumptions.')
    close = False
    if probs is not None:
        top = probs.sort_values(ascending=False)
        close = bool(top.iloc[0] < 0.5 or (top.iloc[0] - top.iloc[1]) < 0.15)
        if top.index[0] != best:
            notes.append(f'The base case favours {best}, but {top.index[0]} wins more often when the inputs are varied.')
    return {'best': best, 'runner_up': order.index[1], 'probs': probs, 'close_call': close,
            'notes': notes, 'criterion': criterion}


def explanation(best, rec, tbl, R, ec: Econ):
    cur = ec.currency
    npv_col = f'NPV ({cur}/kWp)'
    g = R.gain_year
    lines = []
    if best == 'Fixed':
        lines.append('The extra yield of the trackers is too small to cover their extra cost, so the optimised fixed panel is the better choice.')
    else:
        lines.append(f"{best} raises net annual energy by **{tbl.loc[best, 'Net gain vs fixed (%)']:.1f}%** over the best fixed panel "
                     f"(gross gain {g[best].min():.1f}% to {g[best].max():.1f}% across the {len(g)} simulated years).")
    lines.append('Lifetime NPV per kWp: ' + ', '.join(f"{c} {tbl.loc[c, npv_col]:,.0f}" for c in CONFIGS) + f' ({cur}).')
    if rec['probs'] is not None:
        lines.append('Share of 4,000 random scenarios in which each option is best: '
                     + ', '.join(f"{c} {rec['probs'][c] * 100:.0f}%" for c in CONFIGS) + '.')
    if best == 'Single-axis':
        d = tbl.loc['Dual-axis', 'Net energy (kWh/kWp/yr)'] - tbl.loc['Single-axis', 'Net energy (kWh/kWp/yr)']
        lines.append(f"The second axis would add only {d:.0f} kWh/kWp/yr "
                     f"({d / tbl.loc['Single-axis', 'Net energy (kWh/kWp/yr)'] * 100:.1f}%) for a higher cost.")
    return lines
