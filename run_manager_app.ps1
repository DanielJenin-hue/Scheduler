# Launch the manager-first Lab Staffing Scheduler dashboard.
Set-Location $PSScriptRoot
python -m pip install -e ".[app]" -q
python -m streamlit run scripts/manager_app.py --server.port 8501
