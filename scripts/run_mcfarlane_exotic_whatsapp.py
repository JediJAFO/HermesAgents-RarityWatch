"""Windows-safe no-agent cron wrapper for Exotic WhatsApp delivery."""
import subprocess
import sys

result = subprocess.run(
    ["node", "__HERMES_HOME__/scripts/run-mcfarlane-exotic-whatsapp.js"],
    text=True,
    capture_output=True,
    timeout=60,
)
if result.stdout:
    sys.stdout.write(result.stdout)
if result.stderr:
    sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
