# Launch the Lab Staffing Scheduler Streamlit dashboard.
param(
    [switch]$Manager,
    [string]$CompilePeriod
)

Set-Location $PSScriptRoot
python -m pip install -e ".[app]" -q

if ($CompilePeriod) {
    python scripts/compile_period.py --period $CompilePeriod --strict
    exit $LASTEXITCODE
}

if ($Manager) {
    python -m streamlit run scripts/manager_app.py --server.port 8501
} else {
    python -m streamlit run scripts/app.py --server.port 8501
}
