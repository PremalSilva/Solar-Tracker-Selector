# Solar Tracker Selector

A web app that answers one question for any location on Earth:
**fixed panel, single-axis tracker, or dual-axis tracker, which is best here?**

Enter a **latitude and longitude** and the app
1. downloads hourly NASA POWER weather data (or reads a NASA POWER CSV you upload),
2. simulates the three configurations hour by hour with pvlib,
3. compares energy yield year by year, month by month and by sky condition,
4. runs sensitivity studies and lifecycle economics (NPV, LCOE, payback, Monte Carlo),
5. recommends one option and exports the results to Excel / Markdown.

## Quick start

```bash
python -m venv solar-env
solar-env\Scripts\activate          # Windows   (macOS/Linux: source solar-env/bin/activate)
pip install -r requirements.txt
streamlit run app.py
```

The app opens in your browser. Enter the coordinates in the sidebar and press **Run analysis**.

## Files

| File | Purpose |
|---|---|
| `app.py` | The web interface (Streamlit) |
| `engine.py` | All calculations: data download/cleaning, simulation, statistics, economics |
| `requirements.txt` | Python libraries |

## Data

* **Automatic:** NASA POWER hourly API (`ALLSKY_SFC_SW_DWN`, `_DNI`, `_DIFF`, `T2M`, `WS10M`), requested in **UTC**,
  one year per request. An internet connection is required.
* **Upload:** a CSV with either one time column `t` plus `DNI, DHI, GHI, Tair`, or the native NASA layout with
  `YEAR, MO, DY, HR`. Choose the time standard of the file in the sidebar. NASA's default download uses local
  solar time (UTC + round(longitude/15)).
* Years with less than 95 % valid hours are excluded; gaps up to 3 hours are interpolated.

## What the recommendation is based on

You choose the criterion: lifetime value (NPV), cost of energy (LCOE), or energy yield.
4,000 random scenarios (tariff, discount rate, degradation, tracker cost, O&M, motor energy, model uncertainty)
show how robust the result is. **All cost inputs are placeholders; replace them with real quotes.**

## Share it online (free)

Put the three files in a GitHub repository and deploy on Streamlit Community Cloud
(https://streamlit.io/cloud), selecting `app.py` as the main file.

## Limitations

* Single-axis means a horizontal north-south axis with east-west rotation. A tilted axis suits high latitudes better.
* One row of panels: no row-to-row shading or backtracking, no land-use cost.
* NASA POWER is satellite/reanalysis data with coarse resolution (roughly 50 km). Validate against PVGIS or measurements.
* Hourly averages hide short-term cloud variability; spectral effects, soiling and inverter limits are simplified.
* Hourly values are treated as hour-beginning averages (sun evaluated at mid-hour by default). The Data tab
  includes a time-stamp alignment check.
