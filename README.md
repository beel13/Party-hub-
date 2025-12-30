# Party Hub

Party Hub is a single-file Flask app for running party games on a local network. Host on a laptop, players join from their phones, and the host controls rounds from a dashboard.

## Game Modes
- Most Likely To: vote for a player who fits the prompt
- Would You Rather: vote A or B (optionally award points to the majority)
- Trivia: pick the correct answer
- Hot Seat: submit a short answer, host can award points
- Wavelength: guess the secret target from 0 to 100
- Quick Draw: short answers, score unique entries or host-picked winners
- Vote Battle: submit entries, then vote for a winner
- Spyfall Lite: secret roles, find the spy
- Mafia/Werewolf: night/day social deduction

## Requirements
- Python 3.x
- `flask` + `waitress` to run the server
- Optional: `openai` (prompt generation), `qrcode[pil]` (QR join code)

## Quick Start (Windows)
```powershell
py -m venv .venv
.venv\Scripts\activate
pip install flask waitress
py party_server.py --port 5000
```

At startup, the server prints:
- Join URL (share with players)
- Host URL (open on the host laptop)
- Host key (embedded in the host URL)

## How To Play
1. Start the server and share the Join URL with players.
2. Open the Host URL on the host laptop (local access only by default).
3. Pick a mode, start a round, and wait for submissions.
4. Reveal results to award points.

Vote Battle flow: Start Round -> players submit -> host clicks "Start Vote Battle Voting" -> players vote -> host reveals.

## Configuration
Set environment variables before launch:
- `HOST_LOCALONLY`: `true` (default) restricts the host page to localhost. Set to `false` to allow LAN access.
- `OPENAI_API_KEY`: enables AI prompt generation.
- `OPENAI_MODEL`: defaults to `gpt-4o-mini`.

Windows examples:
```powershell
set HOST_LOCALONLY=false
set OPENAI_API_KEY=your_key
set OPENAI_MODEL=gpt-4o-mini
```

## Optional Add-ons
```powershell
pip install openai qrcode[pil]
```

## Testing (no Flask required)
```powershell
py party_server.py --test
```
Flask integration tests are skipped automatically when Flask is not installed.

## Troubleshooting
- Players cannot join: confirm everyone is on the same network and share the Join URL shown in the console.
- Host page blocked: set `HOST_LOCALONLY=false` and restart.
- Missing package errors: reinstall dependencies with `pip install flask waitress`.
- Server won't start and mentions Flask: activate your venv and install Flask + Waitress.
