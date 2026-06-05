# The Home-energy check-up (New Zealand)

A Streamlit-based, gamified public-facing home-energy education and decision-support tool for New Zealand households.

## What changed from the Australian version

This New Zealand version keeps the same GameFi structure, visual style, money-leak framing, mission flow, score system, badges, roadmap and certificate logic, but localises the educational content and calculations to New Zealand.

Key localisation changes:

- Currency changed from AUD/A$ to NZD/NZ$.
- Australian NCC/NatHERS wording replaced with New Zealand Building Code H1 / Healthy Homes framing.
- Heating/cooling content adapted for New Zealand homes, with stronger focus on healthy winter heating, heat pumps, curtains, draughts, insulation, hot water and renter/owner pathways.
- Recommendations adapted to New Zealand public guidance from MBIE Building Performance, Tenancy Services Healthy Homes standards, EECA Energywise advice, and building-performance/retrofit practice.
- Indicative calculation logic converted to New Zealand household energy assumptions.
- The app is standalone: the calculation and recommendation functions are included inside `app.py`.

## Important limitations

This tool is educational decision support only. It is not:

- a certified home energy assessment;
- a NZ Building Code H1 compliance assessment;
- a Healthy Homes compliance statement;
- an accredited building consent or tenancy compliance tool;
- a guaranteed bill forecast.

## Deployment on Streamlit Cloud

1. Upload this folder to a GitHub repository.
2. In Streamlit Cloud, select `app.py` as the main file.
3. Add your company logo at:

```text
assets/company_logo.png
```

If no logo is uploaded, the app will show a placeholder.

## Optional Google Sheets connection

The app can save anonymous completed responses if Streamlit secrets are configured for `streamlit-gsheets`. If the Google Sheets connection is not configured, the app will still run, but response saving will be skipped.

Use `.streamlit/secrets.toml.template` as a starting point.
