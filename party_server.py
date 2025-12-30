# Party Hub (single file)
# Setup (Windows):
#   py -m venv .venv
#   .venv\Scripts\activate
#   pip install flask waitress
# Optional:
#   pip install openai qrcode[pil]
#   set OPENAI_API_KEY=your_key
# Run:
#   py party_server.py --port 5000
# New modes: Wavelength (spectrum guess 0-100), Caption (submit -> vote).
# Caption flow: Start Round -> players submit -> host clicks Start Voting -> players vote -> host Reveal.

from __future__ import annotations

import argparse
import base64
import copy
import io
import json
import os
import random
import secrets
import socket
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, make_response, redirect, render_template_string, request, url_for

try:
    import qrcode  # type: ignore

    HAS_QR = True
except Exception:
    HAS_QR = False

APP_TITLE = "Party Hub"

MODE_LABELS = {
    "mlt": "Most Likely To",
    "wyr": "Would You Rather",
    "trivia": "Trivia",
    "hotseat": "Hot Seat",
    "categories": "Categories",
    "wavelength": "Wavelength",
    "caption": "Caption This",
}

PHASE_LABELS = {
    "lobby": "Lobby",
    "in_round": "In Round",
    "revealed": "Revealed",
}

MODE_DESCRIPTIONS = {
    "mlt": "Vote for a player who best fits the prompt.",
    "wyr": "Pick option A or B.",
    "trivia": "Answer the question. Correct gets a point.",
    "hotseat": "Write a short answer. Host can award a point.",
    "categories": "Write a unique answer. Unique answers score.",
    "wavelength": "Guess the secret target on the spectrum.",
    "caption": "Write a caption, then vote for your favorite.",
}

TEXT_MAX_LEN = 120
AUTO_REFRESH_MS_ACTIVE = 3000
AUTO_REFRESH_MS_REVEALED = 6000


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


HOST_LOCALONLY = env_flag("HOST_LOCALONLY", True)

MLT_PROMPTS: List[str] = [
    "Who is most likely to forget their own birthday?",
    "Who is most likely to start a dance-off?",
    "Who is most likely to be late but still arrive with snacks?",
    "Who is most likely to win a karaoke battle?",
    "Who is most likely to adopt a random pet on impulse?",
    "Who is most likely to trip over nothing?",
    "Who is most likely to fall asleep during a movie?",
    "Who is most likely to organize a last-minute road trip?",
    "Who is most likely to get lost with GPS?",
    "Who is most likely to break their phone screen?",
    "Who is most likely to become famous?",
    "Who is most likely to forget to reply to a text for a week?",
    "Who is most likely to bake cookies at 2 AM?",
    "Who is most likely to start a group chat?",
    "Who is most likely to win a trivia night?",
    "Who is most likely to laugh at their own joke?",
    "Who is most likely to wear mismatched socks?",
    "Who is most likely to volunteer for a scary challenge?",
    "Who is most likely to get a hole-in-one by accident?",
    "Who is most likely to plan the perfect party?",
]

WYR_PROMPTS: List[Dict[str, str]] = [
    {"a": "Have pizza for every meal", "b": "Have tacos for every meal"},
    {"a": "Be able to pause time", "b": "Be able to rewind time"},
    {"a": "Always have to sing instead of speak", "b": "Always have to dance when you walk"},
    {"a": "Live in a treehouse", "b": "Live in a houseboat"},
    {"a": "Never use emojis again", "b": "Only communicate with emojis for a day"},
    {"a": "Have a pet dragon the size of a cat", "b": "Have a cat the size of a dragon"},
    {"a": "Be super fast", "b": "Be super strong"},
    {"a": "Eat spicy food only", "b": "Eat sweet food only"},
    {"a": "Know every language", "b": "Play every instrument"},
    {"a": "Be famous for a meme", "b": "Be famous for a song"},
    {"a": "Always be 10 minutes early", "b": "Always be 10 minutes late"},
    {"a": "Have unlimited snacks", "b": "Have unlimited movies"},
    {"a": "Be able to talk to animals", "b": "Be able to talk to plants"},
    {"a": "Have a rewind button for your day", "b": "Have a skip button for your day"},
    {"a": "Explore the ocean", "b": "Explore space"},
]

TRIVIA_QUESTIONS: List[Dict[str, Any]] = [
    {
        "question": "Which planet is known as the Red Planet?",
        "options": ["Earth", "Mars", "Jupiter", "Venus"],
        "answer_index": 1,
    },
    {
        "question": "How many continents are there on Earth?",
        "options": ["5", "6", "7", "8"],
        "answer_index": 2,
    },
    {
        "question": "What is the largest mammal?",
        "options": ["Elephant", "Blue whale", "Giraffe", "Hippo"],
        "answer_index": 1,
    },
    {
        "question": "Which instrument has 88 keys?",
        "options": ["Guitar", "Violin", "Piano", "Trumpet"],
        "answer_index": 2,
    },
    {
        "question": "What gas do plants breathe in?",
        "options": ["Oxygen", "Carbon dioxide", "Nitrogen", "Helium"],
        "answer_index": 1,
    },
    {
        "question": "Which ocean is the largest?",
        "options": ["Atlantic", "Indian", "Arctic", "Pacific"],
        "answer_index": 3,
    },
    {
        "question": "What is the tallest mountain in the world?",
        "options": ["K2", "Everest", "Kilimanjaro", "Denali"],
        "answer_index": 1,
    },
    {
        "question": "Which sport uses a shuttlecock?",
        "options": ["Tennis", "Badminton", "Squash", "Table tennis"],
        "answer_index": 1,
    },
    {
        "question": "What is the capital of Canada?",
        "options": ["Toronto", "Vancouver", "Ottawa", "Montreal"],
        "answer_index": 2,
    },
    {
        "question": "Which element has the symbol 'O'?",
        "options": ["Gold", "Oxygen", "Osmium", "Zinc"],
        "answer_index": 1,
    },
]

HOTSEAT_PROMPTS: List[str] = [
    "Hot seat: What's your most controversial food opinion?",
    "Hot seat: What's a movie everyone loves that you don't?",
    "Hot seat: What's your most embarrassing habit?",
    "Hot seat: What would you delete from the internet?",
    "Hot seat: What tiny thing makes you unreasonably angry?",
    "Hot seat: What's the worst fashion trend you tried?",
    "Hot seat: What's your guilty pleasure song?",
    "Hot seat: What's a rule you'd make if you ran the world?",
    "Hot seat: What's the weirdest thing you've Googled?",
    "Hot seat: What's a talent you wish you had?",
    "Hot seat: What's a hill you'd die on?",
    "Hot seat: What's the best snack combo?",
    "Hot seat: What's a game you secretly love?",
    "Hot seat: What's your worst travel story?",
    "Hot seat: What's something you pretend to like?",
]

CATEGORIES_PROMPTS: List[str] = [
    "Fast food chain",
    "Superhero name",
    "Anime character",
    "UFC fighter",
    "Car brand",
    "Board game",
    "Pizza topping",
    "Vacation destination",
    "Movie title",
    "City in Europe",
    "Pet name",
    "Candy brand",
    "TV show",
    "Fictional villain",
    "Musical artist",
    "Sport",
    "Ice cream flavor",
    "Book title",
    "Tech company",
    "Animal",
    "Breakfast food",
    "Video game",
    "Clothing brand",
    "Band name",
    "Hobby",
    "Beverage",
    "Celeb nickname",
    "Mythical creature",
    "Kitchen item",
    "App name",
]

SPECTRUM_PROMPTS: List[str] = [
    "Cold <-> Hot",
    "Worst <-> Best",
    "Disgusting <-> Delicious",
    "Boring <-> Exciting",
    "Underrated <-> Overrated",
    "Scary <-> Not scary",
    "Cheap <-> Expensive",
    "Low effort <-> High effort",
    "Quiet <-> Loud",
    "Outdated <-> Trendy",
    "Bad habit <-> Good habit",
    "Chill <-> Intense",
    "Awkward <-> Smooth",
    "Tiny <-> Huge",
    "Clumsy <-> Graceful",
]

CAPTION_PROMPTS: List[str] = [
    "Caption this: you walk into Target and see…",
    "Caption this: the group chat at 2 AM.",
    "Caption this: when the Wi-Fi finally connects.",
    "Caption this: your face when the pizza arrives.",
    "Caption this: trying to act normal in a Zoom call.",
    "Caption this: when the playlist suddenly switches genres.",
    "Caption this: the moment you realize it’s Monday.",
    "Caption this: when the cashier says, “That’ll be free.”",
    "Caption this: when you find money in an old jacket.",
    "Caption this: when your friend says “One more round.”",
    "Caption this: when the dog makes eye contact.",
    "Caption this: when you open the fridge for the 5th time.",
    "Caption this: when your phone is at 1%.",
    "Caption this: when your favorite song comes on.",
    "Caption this: when you remember a random cringe moment.",
]

STATE: Dict[str, Any] = {
    "players": {},
    "scores": {},
    "mode": "mlt",
    "phase": "lobby",
    "round_id": 0,
    "prompt": "",
    "options": [],
    "correct_index": None,
    "wavelength_target": None,
    "submissions": {},
    "caption_phase": None,
    "caption_submissions": {},
    "caption_votes": {},
    "caption_order": [],
    "caption_counter": 0,
    "last_result": None,
    "host_message": "",
    "wyr_points_majority": False,
    "lobby_locked": False,
    "allow_renames": True,
}

STATE_LOCK = threading.Lock()

HOST_KEY = secrets.token_urlsafe(8)

BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }}</title>
    <style>
      :root {
        --bg: #f1f6f9;
        --card: #ffffff;
        --accent: #15616d;
        --accent-2: #ee964b;
        --text: #1b1f24;
        --muted: #5a6872;
        --border: #d6dde4;
      }
      body {
        margin: 0;
        color: var(--text);
        background: linear-gradient(135deg, #f1f6f9 0%, #fff4e4 100%);
        font-family: "Trebuchet MS", "Verdana", sans-serif;
      }
      body.host {
        --bg: #0f0f12;
        --card: #1b1b20;
        --accent: #f4d35e;
        --accent-2: #ee964b;
        --text: #f5f5f5;
        --muted: #b4b9c3;
        --border: #2b2b32;
        background: radial-gradient(circle at top, #1f1f26 0%, #0f0f12 60%);
        font-size: 18px;
      }
      * { box-sizing: border-box; }
      .wrap {
        max-width: 980px;
        margin: 0 auto;
        padding: 24px;
      }
      .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
      }
      body.host .card { box-shadow: none; }
      h1, h2, h3 { margin: 0 0 12px 0; }
      .muted { color: var(--muted); }
      .btn {
        display: inline-block;
        background: var(--accent);
        color: #ffffff;
        border: none;
        padding: 14px 18px;
        border-radius: 12px;
        font-size: 1rem;
        font-weight: 700;
        cursor: pointer;
        text-decoration: none;
      }
      .btn.alt { background: var(--accent-2); }
      .btn.outline {
        background: transparent;
        color: var(--accent);
        border: 2px solid var(--accent);
      }
      body.host .btn {
        font-size: 1.1rem;
        padding: 16px 22px;
      }
      .stack { display: grid; gap: 12px; }
      .stack .btn { width: 100%; }
      .input {
        width: 100%;
        padding: 12px;
        border: 1px solid var(--border);
        border-radius: 10px;
        font-size: 1rem;
      }
      .stats {
        display: flex;
        gap: 16px;
        flex-wrap: wrap;
        margin-top: 8px;
      }
      .stat {
        padding: 10px 14px;
        border-radius: 12px;
        background: rgba(0, 0, 0, 0.04);
        font-weight: 700;
      }
      body.host .stat { background: rgba(255, 255, 255, 0.08); }
      .pill {
        display: inline-block;
        padding: 6px 10px;
        border-radius: 999px;
        background: var(--accent);
        color: #ffffff;
        font-size: 0.8rem;
        font-weight: 700;
      }
      .alert {
        padding: 10px 12px;
        border-radius: 10px;
        background: #ffe9cc;
        color: #553400;
        margin-bottom: 10px;
      }
      body.host .alert {
        background: #4e3b00;
        color: #ffebb3;
      }
      .table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 8px;
      }
      .table th, .table td {
        text-align: left;
        padding: 8px 6px;
        border-bottom: 1px solid var(--border);
      }
      .big { font-size: 2rem; font-weight: 800; }
      .grid-2 { display: grid; gap: 16px; }
      @media (min-width: 860px) {
        .grid-2 { grid-template-columns: 1fr 1fr; }
      }
      .right { text-align: right; }
      .tag {
        font-size: 0.85rem;
        padding: 6px 8px;
        border-radius: 10px;
        background: rgba(0, 0, 0, 0.08);
      }
      body.host .tag { background: rgba(255, 255, 255, 0.12); }
      .options-title { margin: 12px 0 6px 0; font-weight: 700; }
      img { max-width: 220px; height: auto; }
    </style>
  </head>
  <body class="{{ body_class }}">
    <div class="wrap">
      __BODY__
    </div>
  </body>
</html>
"""

JOIN_BODY = """
<div class="card">
  <h1>{{ app_title }}</h1>
  <p class="muted">Join from your phone and enter a display name.</p>
  {% if error %}
  <div class="alert">{{ error }}</div>
  {% endif %}
  <form method="post" action="{{ url_for('join') }}" class="stack">
    <input class="input" type="text" name="name" maxlength="24" placeholder="Display name" required>
    <button class="btn" type="submit">Join the party</button>
  </form>
</div>
<div class="card">
  <h2>Waiting Room</h2>
  <p class="muted">Keep this page open after joining.</p>
</div>
"""

PLAY_BODY = """
<div class="card">
  <div class="grid-2">
    <div>
      <div class="muted">You are</div>
      <div class="big">{{ player_name }}</div>
    </div>
    <div class="right">
      <div class="pill">{{ mode_label }}</div>
      <div class="muted">Round {{ round_id }}</div>
    </div>
  </div>
  {% if message %}
  <div class="alert">{{ message }}</div>
  {% endif %}
  {% if phase == "lobby" %}
    <h2>Waiting for the host...</h2>
    <p class="muted">Round will start soon. Stay on this page.</p>
  {% elif phase == "in_round" %}
    <h2>{{ prompt }}</h2>
    {% if submitted %}
      <p class="tag">Vote received. Waiting for others.</p>
    {% else %}
      <form method="post" action="{{ url_for('submit') }}" class="stack">
        <input type="hidden" name="round_id" value="{{ round_id }}">
        {% if mode == "mlt" %}
          {% for p in player_choices %}
            <button class="btn" type="submit" name="vote" value="{{ p.pid }}">{{ p.name }}</button>
          {% endfor %}
        {% elif mode == "wyr" %}
          <button class="btn" type="submit" name="choice" value="0">A: {{ options[0] }}</button>
          <button class="btn alt" type="submit" name="choice" value="1">B: {{ options[1] }}</button>
        {% elif mode == "trivia" %}
          {% for opt in options %}
            <button class="btn" type="submit" name="choice" value="{{ loop.index0 }}">{{ opt }}</button>
          {% endfor %}
        {% elif mode == "hotseat" or mode == "categories" %}
          <label class="muted">Your answer (max {{ text_max_len }} chars)</label>
          <textarea class="input" name="text_answer" maxlength="{{ text_max_len }}" rows="3" required></textarea>
          <button class="btn" type="submit">Submit</button>
        {% elif mode == "wavelength" %}
          <label class="muted">Your guess (0 - 100)</label>
          <input class="input" type="number" name="wavelength_guess" min="0" max="100" required>
          <button class="btn" type="submit">Submit Guess</button>
        {% elif mode == "caption" %}
          {% if caption_phase == "caption_submit" %}
            <label class="muted">Caption (max {{ text_max_len }} chars)</label>
            <textarea class="input" name="caption_text" maxlength="{{ text_max_len }}" rows="3" required></textarea>
            <button class="btn" type="submit">Submit Caption</button>
          {% elif caption_phase == "caption_vote" %}
            <div class="options-title">Vote for your favorite caption</div>
            {% for choice in caption_choices %}
              {% if choice.is_self %}
                <button class="btn outline" type="button" disabled>Your caption</button>
              {% else %}
                <button class="btn" type="submit" name="caption_vote" value="{{ choice.id }}">{{ choice.text }}</button>
              {% endif %}
            {% endfor %}
          {% else %}
            <p class="muted">Waiting for the host to start voting.</p>
          {% endif %}
        {% endif %}
      </form>
    {% endif %}
  {% elif phase == "revealed" %}
    <h2>Results</h2>
    {% if results and results.mode == "mlt" %}
      <div class="options-title">Top votes: {{ results.max_votes }}</div>
      {% if results.winners %}
        <p class="tag">Winner(s): {{ results.winners|join(", ") }}</p>
      {% else %}
        <p class="muted">No votes were submitted.</p>
      {% endif %}
      <table class="table">
        <tr><th>Player</th><th>Votes</th></tr>
        {% for row in results.tally_rows %}
          <tr><td>{{ row.name }}</td><td>{{ row.votes }}</td></tr>
        {% endfor %}
      </table>
    {% elif results and results.mode == "wyr" %}
      <p class="options-title">A: {{ results.option_a }}</p>
      <p>{{ results.tally_a }} vote(s)</p>
      <p class="options-title">B: {{ results.option_b }}</p>
      <p>{{ results.tally_b }} vote(s)</p>
      {% if results.majority_label %}
        <p class="tag">Majority: {{ results.majority_label }}</p>
      {% else %}
        <p class="muted">Tie vote.</p>
      {% endif %}
    {% elif results and results.mode == "trivia" %}
      <p class="options-title">Correct answer: {{ results.correct_text }}</p>
      <table class="table">
        <tr><th>Option</th><th>Votes</th></tr>
        {% for row in results.option_rows %}
          <tr>
            <td>{{ row.label }}</td>
            <td>{{ row.votes }}</td>
          </tr>
        {% endfor %}
      </table>
    {% elif results and results.mode == "hotseat" %}
      <table class="table">
        <tr><th>Player</th><th>Answer</th></tr>
        {% for row in results.answers %}
          <tr><td>{{ row.name }}</td><td>{{ row.answer }}</td></tr>
        {% endfor %}
      </table>
    {% elif results and results.mode == "categories" %}
      <table class="table">
        <tr><th>Player</th><th>Answer</th><th>Points</th></tr>
        {% for row in results.answers %}
          <tr>
            <td>{{ row.name }}</td>
            <td>{{ row.answer }}</td>
            <td>{{ 1 if row.unique else 0 }}</td>
          </tr>
        {% endfor %}
      </table>
    {% elif results and results.mode == "wavelength" %}
      <p class="options-title">Target: {{ results.target }}</p>
      {% if results.winners %}
        <p class="tag">Closest: {{ results.winners|join(", ") }}</p>
      {% endif %}
      {% if results.average_guess is not none %}
        <p class="muted">Average guess: {{ "%.1f"|format(results.average_guess) }}</p>
      {% endif %}
      <table class="table">
        <tr><th>Player</th><th>Guess</th><th>Distance</th></tr>
        {% for row in results.guesses %}
          <tr>
            <td>{{ row.name }}</td>
            <td>{{ row.guess }}</td>
            <td>{{ row.distance }}</td>
          </tr>
        {% endfor %}
      </table>
    {% elif results and results.mode == "caption" %}
      {% if results.winners %}
        <p class="tag">Winner(s): {{ results.winners|join(", ") }}</p>
      {% else %}
        <p class="muted">No votes submitted.</p>
      {% endif %}
      <table class="table">
        <tr><th>Caption</th><th>Votes</th></tr>
        {% for row in results.captions %}
          <tr>
            <td>{{ row.caption }}</td>
            <td>{{ row.votes }}</td>
          </tr>
        {% endfor %}
      </table>
    {% endif %}
  {% endif %}
</div>

{% if phase == "revealed" %}
<div class="card">
  <h2>Scoreboard</h2>
  {% if scoreboard %}
    <table class="table">
      <tr><th>Player</th><th>Score</th></tr>
      {% for row in scoreboard %}
        <tr><td>{{ row.name }}</td><td>{{ row.score }}</td></tr>
      {% endfor %}
    </table>
  {% else %}
    <p class="muted">No players yet.</p>
  {% endif %}
</div>
{% endif %}

<script>
  setTimeout(function () { window.location.reload(); }, {{ refresh_ms }});
</script>
"""

HOST_BODY = """
<div class="card">
  <h1>MASTER / HOST</h1>
  <div class="stats">
    <div class="stat">Players: <span id="player-count">{{ player_count }}</span></div>
    <div class="stat">Submissions: <span id="submission-count">{{ submission_count }}</span> / <span id="player-count-progress">{{ player_count }}</span></div>
    <div class="stat">Mode: <span id="mode-label">{{ mode_label }}</span></div>
    <div class="stat">Phase: <span id="phase-label">{{ phase_label }}</span></div>
    <div class="stat">Round: <span id="round-id">{{ round_id }}</span></div>
  </div>
  <div class="stats">
    <div class="stat">Lobby: <span id="lobby-lock-status">{{ "Locked" if lobby_locked else "Open" }}</span></div>
    <div class="stat">Renames: <span id="rename-status">{{ "Allowed" if allow_renames else "Locked" }}</span></div>
  </div>
  {% if lobby_locked %}
    <p class="tag">Late joiners blocked</p>
  {% endif %}
  {% if host_message %}
  <div class="alert">{{ host_message }}</div>
  {% endif %}
</div>

<div class="card">
  <h2>Join Link</h2>
  <input class="input" type="text" value="{{ join_url }}" readonly>
  <p class="muted">Host URL (localhost): <span class="tag">{{ host_url }}</span></p>
  {% if join_qr_data %}
    <div style="margin-top:12px;">
      <img src="{{ join_qr_data }}" alt="Join QR">
    </div>
  {% endif %}
</div>

<div class="grid-2">
  <div class="card">
    <h2>Game Hub</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_mode">
      <label class="muted" for="mode">Mode</label>
      <select class="input" name="mode" id="mode">
        {% for key, label in mode_labels.items() %}
          <option value="{{ key }}" {% if key == mode %}selected{% endif %}>{{ label }}</option>
        {% endfor %}
      </select>
      <button class="btn outline" type="submit">Set Mode</button>
    </form>

    <div class="options-title">Mode Guide</div>
    <ul class="muted">
      {% for key, label in mode_labels.items() %}
        <li>{{ label }} - {{ mode_descriptions[key] }}</li>
      {% endfor %}
    </ul>

    <form method="post" action="{{ url_for('host_action') }}" class="stack" style="margin-top:12px;">
      <button class="btn" name="action" value="start_round" type="submit">Start Round</button>
      <button class="btn alt" name="action" value="reveal" type="submit">Reveal Results</button>
      <button class="btn" name="action" value="next_round" type="submit">Next Round</button>
      <button class="btn outline" name="action" value="reset_round" type="submit">Reset Round</button>
      <button class="btn outline" name="action" value="reset_scores" type="submit">Reset Scores</button>
    </form>

    {% if mode == "caption" and phase == "in_round" and caption_phase == "caption_submit" and caption_submit_count > 0 %}
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <button class="btn alt" name="action" value="caption_start_vote" type="submit">Caption: Start Voting</button>
    </form>
    {% endif %}

    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="set_wyr_points">
      <label>
        <input type="checkbox" name="points_majority" {% if wyr_points_majority %}checked{% endif %}>
        Award points to majority vote (WYR)
      </label>
      <div style="margin-top:8px;">
        <button class="btn outline" type="submit">Update WYR Scoring</button>
      </div>
    </form>

    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="toggle_lobby_lock">
      <button class="btn outline" type="submit">
        {% if lobby_locked %}Unlock Lobby{% else %}Lock Lobby{% endif %}
      </button>
    </form>

    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="toggle_allow_renames">
      <button class="btn outline" type="submit">
        {% if allow_renames %}Disable Renames{% else %}Enable Renames{% endif %}
      </button>
    </form>

    <form method="post" action="{{ url_for('host_action') }}" class="stack" style="margin-top:12px;">
      <button class="btn outline" name="action" value="kick_all" type="submit">Kick All Players</button>
    </form>
  </div>

  <div class="card">
    <h2>Current Round</h2>
    <p><span class="muted">Prompt:</span> <span id="prompt-text">{{ prompt or "None" }}</span></p>
    {% if mode == "wavelength" %}
      <p class="muted">Target: <span id="wavelength-target">{{ wavelength_target }}</span></p>
    {% endif %}
    {% if mode == "caption" %}
      <p class="muted">Caption phase: <span id="caption-phase">{{ caption_phase or "caption_submit" }}</span></p>
      <p class="muted">Captions: <span id="caption-submit-count">{{ caption_submit_count }}</span> | Votes: <span id="caption-vote-count">{{ caption_vote_count }}</span></p>
    {% endif %}
    <div class="options-title">Options</div>
    <ul id="options-list">
      {% for opt in options %}
        <li>{{ opt }}</li>
      {% endfor %}
    </ul>
    <p class="muted" id="options-empty"{% if options %} style="display:none;"{% endif %}>No options.</p>
    <div class="options-title">Submitted</div>
    <p class="muted" id="submission-names">
      {% if submission_names %}
        {{ submission_names|join(", ") }}
      {% else %}
        No submissions yet.
      {% endif %}
    </p>
    {% if mode == "trivia" and correct_index is not none %}
      <p class="muted">Correct answer index: {{ correct_index }}</p>
    {% endif %}
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <h2>Players</h2>
    {% if players %}
      <table class="table">
        <tr><th>Name</th><th class="right">Kick</th></tr>
        {% for p in players %}
          <tr>
            <td>{{ p.name }}</td>
            <td class="right">
              <form method="post" action="{{ url_for('host_action') }}">
                <input type="hidden" name="action" value="kick">
                <input type="hidden" name="pid" value="{{ p.pid }}">
                <button class="btn outline" type="submit">Kick</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="muted">No players yet.</p>
    {% endif %}
  </div>

  <div class="card">
    <h2>Scoreboard</h2>
    {% if scoreboard %}
      <table class="table">
        <tr><th>Player</th><th>Score</th></tr>
        {% for row in scoreboard %}
          <tr><td>{{ row.name }}</td><td>{{ row.score }}</td></tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="muted">No scores yet.</p>
    {% endif %}
  </div>
</div>

{% if results %}
<div class="card">
  <h2>Latest Results</h2>
  {% if results.mode == "mlt" %}
    <p class="options-title">Top votes: {{ results.max_votes }}</p>
    {% if results.winners %}
      <p class="tag">Winner(s): {{ results.winners|join(", ") }}</p>
    {% else %}
      <p class="muted">No votes were submitted.</p>
    {% endif %}
    <table class="table">
      <tr><th>Player</th><th>Votes</th></tr>
      {% for row in results.tally_rows %}
        <tr><td>{{ row.name }}</td><td>{{ row.votes }}</td></tr>
      {% endfor %}
    </table>
  {% elif results.mode == "wyr" %}
    <p class="options-title">A: {{ results.option_a }}</p>
    <p>{{ results.tally_a }} vote(s)</p>
    <p class="options-title">B: {{ results.option_b }}</p>
    <p>{{ results.tally_b }} vote(s)</p>
    {% if results.majority_label %}
      <p class="tag">Majority: {{ results.majority_label }}</p>
    {% else %}
      <p class="muted">Tie vote.</p>
    {% endif %}
  {% elif results.mode == "trivia" %}
    <p class="options-title">Correct answer: {{ results.correct_text }}</p>
    <table class="table">
      <tr><th>Option</th><th>Votes</th></tr>
      {% for row in results.option_rows %}
        <tr><td>{{ row.label }}</td><td>{{ row.votes }}</td></tr>
      {% endfor %}
    </table>
  {% elif results.mode == "hotseat" %}
    <table class="table">
      <tr><th>Player</th><th>Answer</th><th class="right">Point</th></tr>
      {% for row in results.answers %}
        <tr>
          <td>{{ row.name }}</td>
          <td>{{ row.answer }}</td>
          <td class="right">
            <form method="post" action="{{ url_for('host_action') }}">
              <input type="hidden" name="action" value="award_point">
              <input type="hidden" name="pid" value="{{ row.pid }}">
              <button class="btn outline" type="submit">Award</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>
  {% elif results.mode == "categories" %}
    <table class="table">
      <tr><th>Player</th><th>Answer</th><th>Points</th></tr>
      {% for row in results.answers %}
        <tr>
          <td>{{ row.name }}</td>
          <td>{{ row.answer }}</td>
          <td>{{ 1 if row.unique else 0 }}</td>
        </tr>
      {% endfor %}
    </table>
  {% elif results.mode == "wavelength" %}
    <p class="options-title">Target: {{ results.target }}</p>
    {% if results.winners %}
      <p class="tag">Closest: {{ results.winners|join(", ") }}</p>
    {% endif %}
    {% if results.average_guess is not none %}
      <p class="muted">Average guess: {{ "%.1f"|format(results.average_guess) }}</p>
    {% endif %}
    <table class="table">
      <tr><th>Player</th><th>Guess</th><th>Distance</th></tr>
      {% for row in results.guesses %}
        <tr>
          <td>{{ row.name }}</td>
          <td>{{ row.guess }}</td>
          <td>{{ row.distance }}</td>
        </tr>
      {% endfor %}
    </table>
  {% elif results.mode == "caption" %}
    {% if results.winners %}
      <p class="tag">Winner(s): {{ results.winners|join(", ") }}</p>
    {% else %}
      <p class="muted">No votes submitted.</p>
    {% endif %}
    <table class="table">
      <tr><th>Caption</th><th>Votes</th><th>Author</th></tr>
      {% for row in results.captions %}
        <tr>
          <td>{{ row.caption }}</td>
          <td>{{ row.votes }}</td>
          <td>{{ row.author or "Hidden" }}</td>
        </tr>
      {% endfor %}
    </table>
  {% endif %}
</div>
{% endif %}

{% if openai_enabled %}
<div class="card">
  <h2>AI Prompt Generation</h2>
  <p class="muted">Generates new prompt pools. Existing rounds are unchanged.</p>
  <form method="post" action="{{ url_for('host_action') }}" class="stack">
    <button class="btn outline" name="action" value="generate_mlt" type="submit">Generate MLT Prompts</button>
    <button class="btn outline" name="action" value="generate_wyr" type="submit">Generate WYR Prompts</button>
    <button class="btn outline" name="action" value="generate_trivia" type="submit">Generate Trivia Questions</button>
    <button class="btn outline" name="action" value="generate_hotseat" type="submit">Generate Hot Seat Prompts</button>
    <button class="btn outline" name="action" value="generate_categories" type="submit">Generate Categories</button>
    <button class="btn outline" name="action" value="generate_wavelength" type="submit">Generate Wavelength Prompts</button>
    <button class="btn outline" name="action" value="generate_caption" type="submit">Generate Caption Prompts</button>
  </form>
</div>
{% endif %}

<script>
  (function () {
    async function poll() {
      try {
        const res = await fetch("{{ url_for('api_state') }}", { cache: "no-store" });
        if (!res.ok) { return; }
        const data = await res.json();
        const playerCount = document.getElementById("player-count");
        const playerCountProgress = document.getElementById("player-count-progress");
        const submissionCount = document.getElementById("submission-count");
        const modeLabel = document.getElementById("mode-label");
        const phaseLabel = document.getElementById("phase-label");
        const roundId = document.getElementById("round-id");
        const promptText = document.getElementById("prompt-text");
        const lobbyLock = document.getElementById("lobby-lock-status");
        const renameStatus = document.getElementById("rename-status");
        const optionsList = document.getElementById("options-list");
        const optionsEmpty = document.getElementById("options-empty");
        const wavelengthTarget = document.getElementById("wavelength-target");
        const captionPhase = document.getElementById("caption-phase");
        const captionSubmitCount = document.getElementById("caption-submit-count");
        const captionVoteCount = document.getElementById("caption-vote-count");
        const submissionNames = document.getElementById("submission-names");
        if (playerCount) { playerCount.textContent = data.player_count; }
        if (playerCountProgress) { playerCountProgress.textContent = data.player_count; }
        if (submissionCount) { submissionCount.textContent = data.submission_count; }
        if (modeLabel) { modeLabel.textContent = data.mode_label || data.mode; }
        if (phaseLabel) { phaseLabel.textContent = data.phase_label || data.phase; }
        if (roundId) { roundId.textContent = data.round_id; }
        if (promptText) { promptText.textContent = data.prompt || "None"; }
        if (lobbyLock) { lobbyLock.textContent = data.lobby_locked ? "Locked" : "Open"; }
        if (renameStatus) { renameStatus.textContent = data.allow_renames ? "Allowed" : "Locked"; }
        if (wavelengthTarget && data.wavelength_target !== null && data.wavelength_target !== undefined) {
          wavelengthTarget.textContent = data.wavelength_target;
        }
        if (captionPhase && data.caption_phase) { captionPhase.textContent = data.caption_phase; }
        if (captionSubmitCount) { captionSubmitCount.textContent = data.caption_submit_count || 0; }
        if (captionVoteCount) { captionVoteCount.textContent = data.caption_vote_count || 0; }
        if (submissionNames && Array.isArray(data.submission_names)) {
          submissionNames.textContent = data.submission_names.length ? data.submission_names.join(", ") : "No submissions yet.";
        }
        if (optionsList && Array.isArray(data.options)) {
          optionsList.innerHTML = "";
          data.options.forEach(function (opt) {
            const li = document.createElement("li");
            li.textContent = opt;
            optionsList.appendChild(li);
          });
          if (optionsEmpty) {
            optionsEmpty.style.display = data.options.length ? "none" : "block";
          }
        }
      } catch (err) {
        return;
      }
    }
    poll();
    setInterval(poll, 2000);
  })();
</script>
"""

HOST_LOCKED_BODY = """
<div class="card">
  <h1>MASTER / HOST</h1>
  <p class="muted">{{ lock_message }}</p>
  <p class="muted">Open this on the laptop: <strong>{{ host_url }}</strong></p>
</div>
"""

def render_page(body: str, *, title: str, body_class: str, **context: Any) -> str:
    template = BASE_TEMPLATE.replace("__BODY__", body)
    return render_template_string(template, title=title, body_class=body_class, **context)


def get_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def is_local_request() -> bool:
    addr = request.remote_addr or ""
    if addr in ("127.0.0.1", "::1"):
        return True
    if addr.startswith("::ffff:127.0.0.1"):
        return True
    return False


def normalize_text(text: str) -> str:
    cleaned = " ".join(text.strip().lower().split())
    for prefix in ("a ", "an ", "the "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def clean_text_answer(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:TEXT_MAX_LEN].strip()


def build_qr_data_url(data: str) -> Optional[str]:
    if not HAS_QR:
        return None
    try:
        qr = qrcode.QRCode(border=1, box_size=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


def get_active_submission_count(state: Dict[str, Any]) -> int:
    mode = state.get("mode")
    if mode == "caption":
        if state.get("caption_phase") == "caption_vote":
            return len(state.get("caption_votes", {}))
        return len(state.get("caption_submissions", {}))
    return len(state.get("submissions", {}))


def get_active_submission_names(state: Dict[str, Any]) -> List[str]:
    players = state.get("players", {})
    mode = state.get("mode")
    if mode == "caption":
        if state.get("caption_phase") == "caption_vote":
            pids = state.get("caption_votes", {}).keys()
        else:
            pids = state.get("caption_submissions", {}).keys()
    else:
        pids = state.get("submissions", {}).keys()
    names = [players.get(pid, {}).get("name", "Unknown") for pid in pids]
    names.sort(key=lambda name: name.lower())
    return names


def build_caption_choices(state: Dict[str, Any], pid: str) -> List[Dict[str, Any]]:
    choices = []
    order = state.get("caption_order", [])
    for entry in order:
        choice_id = entry.get("id")
        author_pid = entry.get("pid")
        text = entry.get("text", "")
        choices.append({"id": choice_id, "text": text, "is_self": author_pid == pid})
    return choices


def openai_ready() -> bool:
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # type: ignore

        _ = openai
    except Exception:
        return False
    return True


def parse_json_from_text(text: str) -> Optional[Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = []
        for line in cleaned.splitlines():
            if line.strip().startswith("```"):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()
    starts = [cleaned.find("["), cleaned.find("{")]
    starts = [idx for idx in starts if idx != -1]
    if starts:
        cleaned = cleaned[min(starts) :]
    end_idx = max(cleaned.rfind("]"), cleaned.rfind("}"))
    if end_idx != -1:
        cleaned = cleaned[: end_idx + 1]
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def call_openai(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY not set."
    try:
        import openai  # type: ignore
    except Exception:
        return None, "openai package not installed."

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    try:
        if hasattr(openai, "OpenAI"):
            client = openai.OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You generate PG-13 party game prompts. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.8,
            )
            content = resp.choices[0].message.content
        else:
            openai.api_key = api_key
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You generate PG-13 party game prompts. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.8,
            )
            content = resp["choices"][0]["message"]["content"]
        return content, None
    except Exception as exc:
        return None, f"OpenAI call failed: {exc}"

def generate_mlt_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 20 PG-13 'Most Likely To' prompts. Return a JSON array of strings. "
        "Do not include player names."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 5:
        return None, "OpenAI returned too few prompts."
    return items, None


def generate_wyr_prompts() -> Tuple[Optional[List[Dict[str, str]]], Optional[str]]:
    prompt = (
        "Create 20 PG-13 'Would you rather' questions. Return a JSON array of objects "
        "with keys 'a' and 'b'. Keep options short. Do not include player names."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    results: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        a = str(item.get("a", "")).strip()
        b = str(item.get("b", "")).strip()
        if a and b:
            results.append({"a": a, "b": b})
    if len(results) < 5:
        return None, "OpenAI returned too few WYR prompts."
    return results, None


def generate_trivia_questions() -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    prompt = (
        "Create 15 PG-13 trivia questions. Return a JSON array of objects with keys "
        "'question', 'options' (array of 4 strings), and 'answer_index' (0-3). "
        "Do not include player names."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    results: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        options = item.get("options")
        answer_index = item.get("answer_index")
        if (
            question
            and isinstance(options, list)
            and len(options) == 4
            and isinstance(answer_index, int)
            and 0 <= answer_index <= 3
        ):
            results.append(
                {"question": question, "options": [str(opt) for opt in options], "answer_index": answer_index}
            )
    if len(results) < 3:
        return None, "OpenAI returned too few trivia questions."
    return results, None


def generate_hotseat_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 20 PG-13 'Hot Seat' prompts. Return a JSON array of strings. "
        "Avoid player names and keep prompts short."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 5:
        return None, "OpenAI returned too few hot seat prompts."
    return items, None


def generate_categories_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 30 PG-13 Categories prompts for a party game. Return a JSON array "
        "of short category strings. Avoid player names."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 10:
        return None, "OpenAI returned too few categories."
    return items, None


def generate_wavelength_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 20 PG-13 spectrum prompts in the form 'X <-> Y'. Return a JSON array of strings. "
        "Keep them short and avoid player names."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 5:
        return None, "OpenAI returned too few wavelength prompts."
    return items, None


def generate_caption_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 20 PG-13 'Caption this' prompts. Return a JSON array of short strings. "
        "Avoid player names and keep prompts punchy."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 5:
        return None, "OpenAI returned too few caption prompts."
    return items, None


def get_state_snapshot() -> Dict[str, Any]:
    with STATE_LOCK:
        return copy.deepcopy(STATE)


def label_for_mode(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


def label_for_phase(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase)


def get_scoreboard(players: Dict[str, Dict[str, str]], scores: Dict[str, int]) -> List[Dict[str, Any]]:
    rows = []
    for pid, info in players.items():
        rows.append({"pid": pid, "name": info.get("name", "Unknown"), "score": scores.get(pid, 0)})
    rows.sort(key=lambda row: (-row["score"], row["name"].lower()))
    return rows

def pick_prompt_for_mode(mode: str) -> Tuple[str, List[str], Optional[int]]:
    if mode == "mlt":
        prompt = random.choice(MLT_PROMPTS) if MLT_PROMPTS else "Who is most likely to plan the next party?"
        return prompt, [], None
    if mode == "wyr":
        if WYR_PROMPTS:
            choice = random.choice(WYR_PROMPTS)
            return "Would you rather...", [choice["a"], choice["b"]], None
        return "Would you rather...", ["Option A", "Option B"], None
    if mode == "trivia":
        if TRIVIA_QUESTIONS:
            question = random.choice(TRIVIA_QUESTIONS)
        else:
            question = {
                "question": "What color is the sky on a clear day?",
                "options": ["Green", "Blue", "Red", "Yellow"],
                "answer_index": 1,
            }
        return question["question"], list(question["options"]), int(question["answer_index"])
    if mode == "hotseat":
        prompt = random.choice(HOTSEAT_PROMPTS) if HOTSEAT_PROMPTS else "Hot seat: Share your hottest take."
        return prompt, [], None
    if mode == "categories":
        category = random.choice(CATEGORIES_PROMPTS) if CATEGORIES_PROMPTS else "Favorite snack"
        return f"Category: {category}", [], None
    if mode == "wavelength":
        prompt = random.choice(SPECTRUM_PROMPTS) if SPECTRUM_PROMPTS else "Cold <-> Hot"
        return prompt, [], None
    if mode == "caption":
        prompt = random.choice(CAPTION_PROMPTS) if CAPTION_PROMPTS else "Caption this: the party has begun."
        return prompt, [], None
    return "Waiting for host", [], None


def start_new_round_locked(mode: str) -> None:
    prompt, options, correct_index = pick_prompt_for_mode(mode)
    STATE["round_id"] += 1
    STATE["mode"] = mode
    STATE["phase"] = "in_round"
    STATE["prompt"] = prompt
    STATE["options"] = options
    STATE["correct_index"] = correct_index
    STATE["wavelength_target"] = random.randint(0, 100) if mode == "wavelength" else None
    STATE["submissions"] = {}
    STATE["caption_phase"] = "caption_submit" if mode == "caption" else None
    STATE["caption_submissions"] = {}
    STATE["caption_votes"] = {}
    STATE["caption_order"] = []
    STATE["caption_counter"] = 0
    STATE["last_result"] = None


def compute_results_locked() -> Dict[str, Any]:
    mode = STATE["mode"]
    players = STATE["players"]
    submissions = STATE["submissions"]
    result: Dict[str, Any] = {
        "mode": mode,
        "round_id": STATE["round_id"],
        "prompt": STATE["prompt"],
        "options": list(STATE["options"]),
    }

    if mode == "mlt":
        tally = {pid: 0 for pid in players}
        for _, target in submissions.items():
            if target in tally:
                tally[target] += 1
        max_votes = max(tally.values()) if tally else 0
        winners = [pid for pid, votes in tally.items() if votes == max_votes and votes > 0]
        for pid in winners:
            STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
        result.update({"tally": tally, "winners": winners, "max_votes": max_votes})

    elif mode == "wyr":
        tally = {0: 0, 1: 0}
        for choice in submissions.values():
            if choice in (0, 1):
                tally[choice] += 1
        majority = None
        if tally[0] > tally[1]:
            majority = 0
        elif tally[1] > tally[0]:
            majority = 1
        if STATE.get("wyr_points_majority") and majority is not None:
            for pid, choice in submissions.items():
                if choice == majority:
                    STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
        result.update(
            {
                "tally": tally,
                "majority": majority,
                "points_majority": STATE.get("wyr_points_majority", False),
            }
        )

    elif mode == "trivia":
        option_count = len(STATE["options"])
        tally = {idx: 0 for idx in range(option_count)}
        for choice in submissions.values():
            if isinstance(choice, int) and choice in tally:
                tally[choice] += 1
        correct = STATE.get("correct_index")
        winners = [pid for pid, choice in submissions.items() if choice == correct]
        for pid in winners:
            STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
        result.update({"tally": tally, "correct_index": correct, "winners": winners})

    elif mode == "hotseat":
        answers = []
        for pid, answer in submissions.items():
            name = players.get(pid, {}).get("name", "Unknown")
            answers.append({"pid": pid, "name": name, "answer": str(answer)})
        result.update({"answers": answers})

    elif mode == "categories":
        answers = []
        normalized_map: Dict[str, List[str]] = {}
        for pid, answer in submissions.items():
            raw = str(answer).strip()
            normalized = normalize_text(raw)
            normalized_map.setdefault(normalized, []).append(pid)
            answers.append({"pid": pid, "name": players.get(pid, {}).get("name", "Unknown"), "answer": raw})

        unique_pids = set()
        for normalized, pids in normalized_map.items():
            if normalized and len(pids) == 1:
                unique_pids.add(pids[0])

        for pid in unique_pids:
            STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1

        result.update({"answers": answers, "unique_pids": list(unique_pids)})

    elif mode == "wavelength":
        target = STATE.get("wavelength_target")
        guesses = []
        for pid, guess in submissions.items():
            try:
                guess_int = int(guess)
            except (TypeError, ValueError):
                continue
            distance = abs(guess_int - target) if isinstance(target, int) else None
            guesses.append(
                {
                    "pid": pid,
                    "name": players.get(pid, {}).get("name", "Unknown"),
                    "guess": guess_int,
                    "distance": distance,
                }
            )
        guesses.sort(key=lambda row: (row["distance"] if row["distance"] is not None else 9999, row["name"].lower()))
        winner_pids: List[str] = []
        if isinstance(target, int) and guesses:
            closest = min(row["distance"] for row in guesses if row["distance"] is not None)
            winner_pids = [row["pid"] for row in guesses if row["distance"] == closest and row["pid"] in players]
            for pid in winner_pids:
                STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
        average_guess = None
        if guesses:
            average_guess = sum(row["guess"] for row in guesses) / len(guesses)
        result.update(
            {
                "target": target,
                "guesses": guesses,
                "winners": winner_pids,
                "average_guess": average_guess,
            }
        )

    elif mode == "caption":
        captions = []
        votes = STATE.get("caption_votes", {})
        order = STATE.get("caption_order", [])
        counts: Dict[int, int] = {entry.get("id"): 0 for entry in order}
        for _, caption_id in votes.items():
            if caption_id in counts:
                counts[caption_id] += 1
        winners: List[str] = []
        if counts:
            max_votes = max(counts.values())
            for entry in order:
                caption_id = entry.get("id")
                if counts.get(caption_id, 0) == max_votes and max_votes > 0:
                    pid = entry.get("pid")
                    if pid in players:
                        winners.append(pid)
        for pid in set(winners):
            STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
        for entry in order:
            caption_id = entry.get("id")
            captions.append(
                {
                    "id": caption_id,
                    "pid": entry.get("pid"),
                    "caption": entry.get("text", ""),
                    "votes": counts.get(caption_id, 0),
                }
            )
        result.update({"captions": captions, "winners": winners})

    STATE["last_result"] = result
    return result


def build_results_view(state: Dict[str, Any], *, reveal_authors: bool = False) -> Optional[Dict[str, Any]]:
    result = state.get("last_result")
    if not result:
        return None
    players = state.get("players", {})
    mode = result.get("mode")
    if mode == "mlt":
        tally = result.get("tally", {})
        rows = []
        for pid, votes in tally.items():
            name = players.get(pid, {}).get("name", "Unknown")
            rows.append({"name": name, "votes": votes})
        rows.sort(key=lambda row: (-row["votes"], row["name"].lower()))
        winners = [players.get(pid, {}).get("name", "Unknown") for pid in result.get("winners", [])]
        return {
            "mode": "mlt",
            "tally_rows": rows,
            "winners": winners,
            "max_votes": result.get("max_votes", 0),
        }
    if mode == "wyr":
        options = result.get("options", ["Option A", "Option B"])
        tally = result.get("tally", {0: 0, 1: 0})
        majority = result.get("majority")
        majority_label = None
        if majority in (0, 1):
            majority_label = "A" if majority == 0 else "B"
        return {
            "mode": "wyr",
            "option_a": options[0] if len(options) > 0 else "Option A",
            "option_b": options[1] if len(options) > 1 else "Option B",
            "tally_a": tally.get(0, 0),
            "tally_b": tally.get(1, 0),
            "majority_label": majority_label,
        }
    if mode == "trivia":
        options = result.get("options", [])
        tally = result.get("tally", {})
        correct = result.get("correct_index")
        rows = []
        for idx, opt in enumerate(options):
            label = opt
            if idx == correct:
                label = f"{opt} (correct)"
            rows.append({"label": label, "votes": tally.get(idx, 0)})
        correct_text = options[correct] if isinstance(correct, int) and 0 <= correct < len(options) else "Unknown"
        return {"mode": "trivia", "option_rows": rows, "correct_text": correct_text}
    if mode == "hotseat":
        answers = []
        for row in result.get("answers", []):
            answers.append(
                {
                    "pid": row.get("pid"),
                    "name": row.get("name", "Unknown"),
                    "answer": row.get("answer", ""),
                }
            )
        answers.sort(key=lambda row: row["name"].lower())
        return {"mode": "hotseat", "answers": answers}
    if mode == "categories":
        answers = []
        unique_pids = set(result.get("unique_pids", []))
        for row in result.get("answers", []):
            pid = row.get("pid")
            answers.append(
                {
                    "pid": pid,
                    "name": row.get("name", "Unknown"),
                    "answer": row.get("answer", ""),
                    "unique": pid in unique_pids,
                }
            )
        answers.sort(key=lambda row: row["name"].lower())
        return {"mode": "categories", "answers": answers}
    if mode == "wavelength":
        guesses = []
        for row in result.get("guesses", []):
            pid = row.get("pid")
            guesses.append(
                {
                    "pid": pid,
                    "name": row.get("name", "Unknown"),
                    "guess": row.get("guess"),
                    "distance": row.get("distance"),
                }
            )
        guesses.sort(key=lambda row: (row["distance"] if row["distance"] is not None else 9999, row["name"].lower()))
        winners = [players.get(pid, {}).get("name", "Unknown") for pid in result.get("winners", [])]
        return {
            "mode": "wavelength",
            "target": result.get("target"),
            "guesses": guesses,
            "winners": winners,
            "average_guess": result.get("average_guess"),
        }
    if mode == "caption":
        captions = []
        winners = set(result.get("winners", []))
        for row in result.get("captions", []):
            pid = row.get("pid")
            caption_entry = {
                "caption": row.get("caption", ""),
                "votes": row.get("votes", 0),
                "winner": pid in winners,
            }
            if reveal_authors:
                caption_entry["author"] = players.get(pid, {}).get("name", "Unknown")
            captions.append(caption_entry)
        captions.sort(key=lambda row: (-row["votes"], row["caption"].lower()))
        winner_names = [players.get(pid, {}).get("name", "Unknown") for pid in winners]
        return {"mode": "caption", "captions": captions, "winners": winner_names}
    return None


def is_host_request() -> bool:
    if request.cookies.get("host") != HOST_KEY:
        return False
    if HOST_LOCALONLY and not is_local_request():
        return False
    return True

app = Flask(__name__)


@app.get("/")
def index() -> str:
    pid = request.cookies.get("pid")
    snapshot = get_state_snapshot()
    if pid and pid in snapshot.get("players", {}):
        return redirect(url_for("play"))
    error = request.args.get("error")
    return render_page(JOIN_BODY, title=APP_TITLE, body_class="", app_title=APP_TITLE, error=error)


@app.post("/join")
def join() -> Any:
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect(url_for("index", error="Display name is required."))
    pid = request.cookies.get("pid") or str(uuid.uuid4())
    with STATE_LOCK:
        if pid not in STATE["players"]:
            if STATE.get("lobby_locked"):
                return redirect(url_for("index", error="Lobby is locked."))
            STATE["players"][pid] = {"name": name}
            STATE["scores"][pid] = 0
        else:
            if not STATE.get("allow_renames", True) and name != STATE["players"][pid].get("name"):
                return redirect(url_for("play", msg="Name changes are disabled."))
            STATE["players"][pid]["name"] = name
        if pid not in STATE["scores"]:
            STATE["scores"][pid] = 0
    resp = make_response(redirect(url_for("play")))
    resp.set_cookie("pid", pid, max_age=60 * 60 * 24 * 30, samesite="Lax", httponly=True)
    return resp


@app.get("/play")
def play() -> str:
    pid = request.cookies.get("pid")
    snapshot = get_state_snapshot()
    player = snapshot.get("players", {}).get(pid or "")
    if not player:
        return redirect(url_for("index"))
    mode = snapshot.get("mode")
    caption_phase = snapshot.get("caption_phase")
    if mode == "caption":
        if caption_phase == "caption_vote":
            submitted = pid in snapshot.get("caption_votes", {})
        else:
            submitted = pid in snapshot.get("caption_submissions", {})
    else:
        submitted = pid in snapshot.get("submissions", {})
    refresh_ms = AUTO_REFRESH_MS_REVEALED if snapshot.get("phase") == "revealed" else AUTO_REFRESH_MS_ACTIVE
    player_choices = []
    for player_id, info in snapshot.get("players", {}).items():
        player_choices.append({"pid": player_id, "name": info.get("name", "Unknown")})
    player_choices.sort(key=lambda row: row["name"].lower())
    results_view = build_results_view(snapshot, reveal_authors=False) if snapshot.get("phase") == "revealed" else None
    scoreboard = get_scoreboard(snapshot.get("players", {}), snapshot.get("scores", {}))
    message = request.args.get("msg")
    caption_choices = []
    if mode == "caption" and caption_phase == "caption_vote":
        caption_choices = build_caption_choices(snapshot, pid)

    return render_page(
        PLAY_BODY,
        title=f"{APP_TITLE} - Play",
        body_class="",
        player_name=player.get("name", "Player"),
        mode=mode,
        mode_label=label_for_mode(mode or ""),
        phase=snapshot.get("phase"),
        prompt=snapshot.get("prompt", ""),
        options=snapshot.get("options", []),
        round_id=snapshot.get("round_id", 0),
        submitted=submitted,
        player_choices=player_choices,
        results=results_view,
        scoreboard=scoreboard,
        message=message,
        refresh_ms=refresh_ms,
        text_max_len=TEXT_MAX_LEN,
        caption_phase=caption_phase,
        caption_choices=caption_choices,
    )


@app.post("/submit")
def submit() -> Any:
    pid = request.cookies.get("pid")
    if not pid:
        return redirect(url_for("index"))

    round_id_raw = request.form.get("round_id", "")
    try:
        round_id = int(round_id_raw)
    except ValueError:
        round_id = -1

    with STATE_LOCK:
        if pid not in STATE["players"]:
            return redirect(url_for("index"))
        if STATE["phase"] != "in_round":
            return redirect(url_for("play", msg="Round is not active."))
        if round_id != STATE["round_id"]:
            return redirect(url_for("play", msg="Round has changed."))

        mode = STATE["mode"]
        if mode == "caption":
            caption_phase = STATE.get("caption_phase")
            if caption_phase == "caption_submit":
                if pid in STATE.get("caption_submissions", {}):
                    return redirect(url_for("play", msg="Already submitted."))
                text_raw = request.form.get("caption_text", "")
                text = clean_text_answer(text_raw)
                if not text:
                    return redirect(url_for("play", msg="Caption cannot be empty."))
                STATE["caption_submissions"][pid] = text
                caption_id = STATE.get("caption_counter", 0)
                STATE["caption_counter"] = caption_id + 1
                STATE["caption_order"].append({"id": caption_id, "pid": pid, "text": text})
            elif caption_phase == "caption_vote":
                if pid in STATE.get("caption_votes", {}):
                    return redirect(url_for("play", msg="Already voted."))
                choice_raw = request.form.get("caption_vote", "")
                try:
                    caption_id = int(choice_raw)
                except ValueError:
                    return redirect(url_for("play", msg="Invalid selection."))
                order = STATE.get("caption_order", [])
                entry = next((item for item in order if item.get("id") == caption_id), None)
                if entry is None:
                    return redirect(url_for("play", msg="Invalid selection."))
                if entry.get("pid") == pid:
                    return redirect(url_for("play", msg="You cannot vote for your own caption."))
                STATE["caption_votes"][pid] = caption_id
            else:
                return redirect(url_for("play", msg="Caption voting is not active."))
            return redirect(url_for("play"))

        if pid in STATE["submissions"]:
            return redirect(url_for("play", msg="Already submitted."))

        if mode == "mlt":
            target = request.form.get("vote")
            if target not in STATE["players"]:
                return redirect(url_for("play", msg="Invalid selection."))
            STATE["submissions"][pid] = target
        elif mode in ("wyr", "trivia"):
            choice_raw = request.form.get("choice", "")
            try:
                choice = int(choice_raw)
            except ValueError:
                return redirect(url_for("play", msg="Invalid selection."))
            if mode == "wyr" and choice not in (0, 1):
                return redirect(url_for("play", msg="Invalid selection."))
            if mode == "trivia" and (choice < 0 or choice >= len(STATE["options"])):
                return redirect(url_for("play", msg="Invalid selection."))
            STATE["submissions"][pid] = choice
        elif mode in ("hotseat", "categories"):
            text_raw = request.form.get("text_answer", "")
            text = clean_text_answer(text_raw)
            if not text:
                return redirect(url_for("play", msg="Answer cannot be empty."))
            STATE["submissions"][pid] = text
        elif mode == "wavelength":
            guess_raw = request.form.get("wavelength_guess", "")
            try:
                guess = int(guess_raw)
            except ValueError:
                return redirect(url_for("play", msg="Invalid guess."))
            if guess < 0 or guess > 100:
                return redirect(url_for("play", msg="Guess must be 0 to 100."))
            STATE["submissions"][pid] = guess
        else:
            return redirect(url_for("play", msg="Unknown mode."))

    return redirect(url_for("play"))

@app.get("/host")
def host() -> Any:
    key = request.args.get("key")
    join_url = app.config.get("JOIN_URL", "")
    host_url = app.config.get("HOST_URL", "")
    if key:
        if not is_local_request():
            return render_page(
                HOST_LOCKED_BODY,
                title=f"{APP_TITLE} - Host",
                body_class="host",
                lock_message="Host key can only be used from the laptop (localhost).",
                host_url=host_url,
            )
        if key == HOST_KEY:
            resp = make_response(redirect(url_for("host")))
            resp.set_cookie("host", HOST_KEY, httponly=True, samesite="Lax")
            return resp
        return render_page(
            HOST_LOCKED_BODY,
            title=f"{APP_TITLE} - Host",
            body_class="host",
            lock_message="Invalid host key. Use the printed host URL on the laptop.",
            host_url=host_url,
        )
    if HOST_LOCALONLY and not is_local_request():
        return render_page(
            HOST_LOCKED_BODY,
            title=f"{APP_TITLE} - Host",
            body_class="host",
            lock_message="Host access is locked to the laptop. Open the host URL on localhost.",
            host_url=host_url,
        )
    if not is_host_request():
        return render_page(
            HOST_LOCKED_BODY,
            title=f"{APP_TITLE} - Host",
            body_class="host",
            lock_message="Host access requires the host key. Use the printed host URL on the laptop.",
            host_url=host_url,
        )

    snapshot = get_state_snapshot()
    join_qr_data = build_qr_data_url(join_url) if join_url else None
    players = []
    for pid, info in snapshot.get("players", {}).items():
        players.append({"pid": pid, "name": info.get("name", "Unknown")})
    players.sort(key=lambda row: row["name"].lower())
    scoreboard = get_scoreboard(snapshot.get("players", {}), snapshot.get("scores", {}))
    results_view = build_results_view(snapshot, reveal_authors=True) if snapshot.get("phase") == "revealed" else None
    submission_count = get_active_submission_count(snapshot)
    submission_names = get_active_submission_names(snapshot)
    caption_submit_count = len(snapshot.get("caption_submissions", {}))
    caption_vote_count = len(snapshot.get("caption_votes", {}))
    return render_page(
        HOST_BODY,
        title=f"{APP_TITLE} - Host",
        body_class="host",
        player_count=len(snapshot.get("players", {})),
        submission_count=submission_count,
        mode=snapshot.get("mode"),
        mode_label=label_for_mode(snapshot.get("mode", "")),
        phase=snapshot.get("phase"),
        phase_label=label_for_phase(snapshot.get("phase", "")),
        round_id=snapshot.get("round_id", 0),
        prompt=snapshot.get("prompt", ""),
        options=snapshot.get("options", []),
        correct_index=snapshot.get("correct_index"),
        wavelength_target=snapshot.get("wavelength_target"),
        caption_phase=snapshot.get("caption_phase"),
        caption_submit_count=caption_submit_count,
        caption_vote_count=caption_vote_count,
        submission_names=submission_names,
        players=players,
        scoreboard=scoreboard,
        results=results_view,
        host_message=snapshot.get("host_message", ""),
        lobby_locked=snapshot.get("lobby_locked", False),
        allow_renames=snapshot.get("allow_renames", True),
        openai_enabled=openai_ready(),
        mode_labels=MODE_LABELS,
        mode_descriptions=MODE_DESCRIPTIONS,
        wyr_points_majority=snapshot.get("wyr_points_majority", False),
        join_url=join_url,
        host_url=host_url,
        join_qr_data=join_qr_data,
    )


@app.post("/host/action")
def host_action() -> Any:
    if not is_host_request():
        return "Host access required.", 403

    action = request.form.get("action", "")
    if action == "generate_mlt":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        prompts, err = generate_mlt_prompts()
        with STATE_LOCK:
            if prompts:
                global MLT_PROMPTS
                MLT_PROMPTS = prompts
                STATE["host_message"] = f"Generated {len(prompts)} MLT prompts."
            else:
                STATE["host_message"] = err or "Failed to generate prompts."
        return redirect(url_for("host"))

    if action == "generate_wyr":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        prompts, err = generate_wyr_prompts()
        with STATE_LOCK:
            if prompts:
                global WYR_PROMPTS
                WYR_PROMPTS = prompts
                STATE["host_message"] = f"Generated {len(prompts)} WYR prompts."
            else:
                STATE["host_message"] = err or "Failed to generate prompts."
        return redirect(url_for("host"))

    if action == "generate_trivia":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        questions, err = generate_trivia_questions()
        with STATE_LOCK:
            if questions:
                global TRIVIA_QUESTIONS
                TRIVIA_QUESTIONS = questions
                STATE["host_message"] = f"Generated {len(questions)} trivia questions."
            else:
                STATE["host_message"] = err or "Failed to generate trivia questions."
        return redirect(url_for("host"))

    if action == "generate_hotseat":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        prompts, err = generate_hotseat_prompts()
        with STATE_LOCK:
            if prompts:
                global HOTSEAT_PROMPTS
                HOTSEAT_PROMPTS = prompts
                STATE["host_message"] = f"Generated {len(prompts)} hot seat prompts."
            else:
                STATE["host_message"] = err or "Failed to generate hot seat prompts."
        return redirect(url_for("host"))

    if action == "generate_categories":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        prompts, err = generate_categories_prompts()
        with STATE_LOCK:
            if prompts:
                global CATEGORIES_PROMPTS
                CATEGORIES_PROMPTS = prompts
                STATE["host_message"] = f"Generated {len(prompts)} categories."
            else:
                STATE["host_message"] = err or "Failed to generate categories."
        return redirect(url_for("host"))

    if action == "generate_wavelength":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        prompts, err = generate_wavelength_prompts()
        with STATE_LOCK:
            if prompts:
                global SPECTRUM_PROMPTS
                SPECTRUM_PROMPTS = prompts
                STATE["host_message"] = f"Generated {len(prompts)} wavelength prompts."
            else:
                STATE["host_message"] = err or "Failed to generate wavelength prompts."
        return redirect(url_for("host"))

    if action == "generate_caption":
        if not openai_ready():
            with STATE_LOCK:
                STATE["host_message"] = "OpenAI is not configured."
            return redirect(url_for("host"))
        prompts, err = generate_caption_prompts()
        with STATE_LOCK:
            if prompts:
                global CAPTION_PROMPTS
                CAPTION_PROMPTS = prompts
                STATE["host_message"] = f"Generated {len(prompts)} caption prompts."
            else:
                STATE["host_message"] = err or "Failed to generate caption prompts."
        return redirect(url_for("host"))

    with STATE_LOCK:
        STATE["host_message"] = ""
        if action == "set_mode":
            mode = request.form.get("mode", "mlt")
            if STATE["phase"] == "in_round":
                STATE["host_message"] = "Cannot change mode during an active round."
            elif mode in MODE_LABELS:
                STATE["mode"] = mode
                if mode != "caption":
                    STATE["caption_phase"] = None
                STATE["host_message"] = f"Mode set to {label_for_mode(mode)}."
            else:
                STATE["host_message"] = "Unknown mode."

        elif action == "start_round":
            if STATE["phase"] == "in_round":
                STATE["host_message"] = "Round already in progress."
            elif not STATE["players"]:
                STATE["host_message"] = "No players yet."
            else:
                start_new_round_locked(STATE["mode"])
                STATE["host_message"] = "Round started."

        elif action == "reveal":
            if STATE["phase"] != "in_round":
                STATE["host_message"] = "No active round to reveal."
            elif STATE["mode"] == "caption" and STATE.get("caption_phase") != "caption_vote":
                STATE["host_message"] = "Start caption voting before revealing."
            else:
                compute_results_locked()
                STATE["phase"] = "revealed"
                STATE["host_message"] = "Results revealed."

        elif action == "next_round":
            if STATE["phase"] == "in_round":
                STATE["host_message"] = "Reveal results before starting next round."
            elif not STATE["players"]:
                STATE["host_message"] = "No players yet."
            else:
                start_new_round_locked(STATE["mode"])
                STATE["host_message"] = "Next round started."

        elif action == "reset_round":
            STATE["phase"] = "lobby"
            STATE["prompt"] = ""
            STATE["options"] = []
            STATE["correct_index"] = None
            STATE["wavelength_target"] = None
            STATE["submissions"] = {}
            STATE["caption_phase"] = None
            STATE["caption_submissions"] = {}
            STATE["caption_votes"] = {}
            STATE["caption_order"] = []
            STATE["caption_counter"] = 0
            STATE["last_result"] = None
            STATE["host_message"] = "Round reset."

        elif action == "reset_scores":
            for pid in list(STATE["scores"].keys()):
                STATE["scores"][pid] = 0
            STATE["phase"] = "lobby"
            STATE["prompt"] = ""
            STATE["options"] = []
            STATE["correct_index"] = None
            STATE["wavelength_target"] = None
            STATE["submissions"] = {}
            STATE["caption_phase"] = None
            STATE["caption_submissions"] = {}
            STATE["caption_votes"] = {}
            STATE["caption_order"] = []
            STATE["caption_counter"] = 0
            STATE["last_result"] = None
            STATE["host_message"] = "Scores reset."

        elif action == "kick":
            pid = request.form.get("pid")
            if pid and pid in STATE["players"]:
                STATE["players"].pop(pid, None)
                STATE["scores"].pop(pid, None)
                STATE["submissions"].pop(pid, None)
                STATE["caption_submissions"].pop(pid, None)
                STATE["caption_votes"].pop(pid, None)
                removed_ids = {entry.get("id") for entry in STATE["caption_order"] if entry.get("pid") == pid}
                STATE["caption_order"] = [entry for entry in STATE["caption_order"] if entry.get("pid") != pid]
                if removed_ids:
                    STATE["caption_votes"] = {
                        voter: vote for voter, vote in STATE["caption_votes"].items() if vote not in removed_ids
                    }
                STATE["host_message"] = "Player removed."
            else:
                STATE["host_message"] = "Player not found."

        elif action == "kick_all":
            STATE["players"] = {}
            STATE["scores"] = {}
            STATE["submissions"] = {}
            STATE["phase"] = "lobby"
            STATE["prompt"] = ""
            STATE["options"] = []
            STATE["correct_index"] = None
            STATE["wavelength_target"] = None
            STATE["caption_phase"] = None
            STATE["caption_submissions"] = {}
            STATE["caption_votes"] = {}
            STATE["caption_order"] = []
            STATE["caption_counter"] = 0
            STATE["last_result"] = None
            STATE["round_id"] = 0
            STATE["host_message"] = "All players removed."

        elif action == "set_wyr_points":
            STATE["wyr_points_majority"] = request.form.get("points_majority") == "on"
            STATE["host_message"] = "WYR scoring updated."

        elif action == "toggle_lobby_lock":
            STATE["lobby_locked"] = not STATE.get("lobby_locked", False)
            STATE["host_message"] = "Lobby locked." if STATE["lobby_locked"] else "Lobby unlocked."

        elif action == "toggle_allow_renames":
            STATE["allow_renames"] = not STATE.get("allow_renames", True)
            STATE["host_message"] = "Renames enabled." if STATE["allow_renames"] else "Renames disabled."

        elif action == "caption_start_vote":
            if STATE["mode"] != "caption":
                STATE["host_message"] = "Caption voting is only for Caption mode."
            elif STATE["phase"] != "in_round":
                STATE["host_message"] = "No active round."
            elif STATE.get("caption_phase") != "caption_submit":
                STATE["host_message"] = "Caption voting already started."
            elif not STATE.get("caption_submissions"):
                STATE["host_message"] = "No captions submitted yet."
            else:
                STATE["caption_phase"] = "caption_vote"
                STATE["host_message"] = "Caption voting started."

        elif action == "award_point":
            pid = request.form.get("pid")
            if STATE["phase"] != "revealed":
                STATE["host_message"] = "Points can only be awarded after reveal."
            elif STATE["mode"] != "hotseat":
                STATE["host_message"] = "Award points is only for Hot Seat."
            elif pid and pid in STATE["players"]:
                STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
                STATE["host_message"] = "Point awarded."
            else:
                STATE["host_message"] = "Player not found."

        else:
            STATE["host_message"] = "Unknown action."

    return redirect(url_for("host"))


@app.get("/api/state")
def api_state() -> Any:
    if not is_host_request():
        return jsonify({"error": "host required"}), 403
    snapshot = get_state_snapshot()
    return jsonify(
        {
            "player_count": len(snapshot.get("players", {})),
            "submission_count": get_active_submission_count(snapshot),
            "submission_names": get_active_submission_names(snapshot),
            "mode": snapshot.get("mode"),
            "mode_label": label_for_mode(snapshot.get("mode", "")),
            "phase": snapshot.get("phase"),
            "phase_label": label_for_phase(snapshot.get("phase", "")),
            "round_id": snapshot.get("round_id", 0),
            "prompt": snapshot.get("prompt", ""),
            "options": snapshot.get("options", []),
            "lobby_locked": snapshot.get("lobby_locked", False),
            "allow_renames": snapshot.get("allow_renames", True),
            "wavelength_target": snapshot.get("wavelength_target"),
            "caption_phase": snapshot.get("caption_phase"),
            "caption_submit_count": len(snapshot.get("caption_submissions", {})),
            "caption_vote_count": len(snapshot.get("caption_votes", {})),
        }
    )

def print_startup_info(port: int) -> Tuple[str, str]:
    ip = get_lan_ip()
    join_url = f"http://{ip}:{port}"
    host_url = f"http://localhost:{port}/host?key={HOST_KEY}"
    print("=" * 60)
    print("Party Hub - Quickstart (Windows)")
    print("Setup:")
    print("  py -m venv .venv")
    print("  .venv\\Scripts\\activate")
    print("  pip install flask waitress")
    print("Optional:")
    print("  pip install openai qrcode[pil]")
    print("  set OPENAI_API_KEY=your_key")
    print("Networking tips:")
    print("  ipconfig  (look for IPv4 Address)")
    print("  Allow Windows Firewall prompt for Private networks")
    print("-" * 60)
    print(f"Join URL: {join_url}")
    print(f"Host URL: {host_url}")
    print(f"Host key: {HOST_KEY}")
    print("=" * 60)
    if HAS_QR:
        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(join_url)
            qr.make(fit=True)
            print("Scan to join:")
            for row in qr.get_matrix():
                line = "".join(["##" if cell else "  " for cell in row])
                print(line)
            print("=" * 60)
        except Exception:
            print("QR code available but could not render.")
    return join_url, host_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Party Hub server")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    args = parser.parse_args()

    join_url, host_url = print_startup_info(args.port)
    app.config["JOIN_URL"] = join_url
    app.config["HOST_URL"] = host_url
    app.config["PORT"] = args.port

    try:
        from waitress import serve  # type: ignore
    except Exception:
        print("Waitress is not installed. Run: pip install waitress")
        raise SystemExit(1)

    serve(app, host="0.0.0.0", port=args.port, threads=8)


if __name__ == "__main__":
    main()
