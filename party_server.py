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
# New features: timer auto-advance + locking, teams, lobby code + reclaim, profanity filter,
# recap export, Spyfall Lite + Mafia/Werewolf, public polling, and UI polish.
# Spyfall: Start round -> players see roles -> host clicks "Start Spy Vote" -> Reveal Results.
# Mafia: Start round (night) -> werewolves pick + seer inspects -> host "Resolve Night/Start Day"
#        -> day vote -> host "Resolve Day" (repeat) -> End Game/Next Round.
# Tests: py party_server.py --test  OR  py -m unittest party_server

from __future__ import annotations

import argparse
import base64
import copy
import datetime
import io
import json
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
import uuid
import unittest
from typing import Any, Dict, List, Optional, Tuple

# Flask is optional for --test; FLASK_AVAILABLE gates route registration.
# Run unit tests without Flask via: py party_server.py --test
try:
    from flask import Flask, jsonify, make_response, redirect, render_template_string, request, url_for

    FLASK_AVAILABLE = True
except ModuleNotFoundError:
    FLASK_AVAILABLE = False
    Flask = None  # type: ignore[assignment]
    jsonify = None  # type: ignore[assignment]
    make_response = None  # type: ignore[assignment]
    redirect = None  # type: ignore[assignment]
    render_template_string = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    url_for = None  # type: ignore[assignment]

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
    "trivia_buzzer": "Trivia Buzzer",
    "team_trivia": "Team Trivia Buzzer",
    "team_jeopardy": "Team Jeopardy",
    "relay_trivia": "Relay Trivia",
    "trivia_draft": "Trivia Draft",
    "wager_trivia": "Wager Trivia",
    "estimation_duel": "Estimation Duel",
    "hotseat": "Hot Seat",
    "wavelength": "Wavelength",
    "quickdraw": "Quick Draw",
    "votebattle": "Vote Battle",
    "spyfall": "Spyfall Lite",
    "mafia": "Mafia/Werewolf",
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
    "trivia_buzzer": "Buzz in first to answer and steal points.",
    "team_trivia": "Teams buzz in and answer first.",
    "team_jeopardy": "Pick clues, buzz in, and score by team.",
    "relay_trivia": "Captains rotate and answer for their team.",
    "trivia_draft": "Draft questions, answer your picks, and steal.",
    "wager_trivia": "Wager points before answering.",
    "estimation_duel": "Closest estimate wins the duel.",
    "hotseat": "Write a short answer. Host can award a point.",
    "wavelength": "Guess the secret target on the spectrum.",
    "quickdraw": "Short answer challenge. Unique or host-picked wins.",
    "votebattle": "Submit an entry, then vote for your favorite.",
    "spyfall": "Secret roles. Find the spy, then vote.",
    "mafia": "Night/day social deduction. Werewolves vs villagers.",
}

TEXT_MAX_LEN = 120
QUICKDRAW_MAX_LEN = 40
VOTEBATTLE_MAX_LEN = 80
NAME_MAX_LEN = 24
PUBLIC_POLL_MS = 2500
HOST_POLL_MS = 2000
HOST_TIMER_POLL_MS = 1000
JOIN_CODE_LENGTH = 5
TIMER_DEFAULT_SECONDS = 45
VOTE_TIMER_DEFAULT_SECONDS = 30


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except OSError:
        return


load_dotenv()
HOST_LOCALONLY = env_flag("HOST_LOCALONLY", True)
LOBBY_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
BANNED_WORDS_MILD = {
    "crap",
    "damn",
    "darn",
    "hell",
}
BANNED_WORDS_STRICT = BANNED_WORDS_MILD.union(
    {
        "asshole",
        "bastard",
        "bitch",
        "fuck",
        "shit",
    }
)
MAFIA_MIN_PLAYERS = 3

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

ESTIMATION_PROMPTS: List[Dict[str, Any]] = [
    {"prompt": "Number of bones in the adult human body", "target": 206},
    {"prompt": "Minutes in a day", "target": 1440},
    {"prompt": "Number of keys on a standard piano", "target": 88},
    {"prompt": "Number of letters in the English alphabet", "target": 26},
    {"prompt": "Number of stars on the US flag", "target": 50},
    {"prompt": "Number of UN member countries", "target": 193},
    {"prompt": "Height of Mount Everest in meters", "target": 8848},
    {"prompt": "Players on the field for one soccer team", "target": 11},
]

JEOPARDY_CATEGORIES: List[Dict[str, Any]] = [
    {
        "category": "Space",
        "clues": [
            {"question": "This planet is known as the Red Planet.", "answer": "Mars"},
            {"question": "Our galaxy is called the Milky Way.", "answer": "Milky Way"},
            {"question": "Planet with the most visible rings.", "answer": "Saturn"},
            {"question": "The first person to walk on the Moon.", "answer": "Neil Armstrong"},
            {"question": "The star at the center of our solar system.", "answer": "Sun"},
        ],
    },
    {
        "category": "Geography",
        "clues": [
            {"question": "The longest river in the world.", "answer": "Nile"},
            {"question": "Country shaped like a boot.", "answer": "Italy"},
            {"question": "The largest desert on Earth.", "answer": "Sahara"},
            {"question": "Capital city of Japan.", "answer": "Tokyo"},
            {"question": "The tallest mountain in the world.", "answer": "Everest"},
        ],
    },
    {
        "category": "Science",
        "clues": [
            {"question": "H2O is the chemical formula for this.", "answer": "Water"},
            {"question": "The process plants use to make food.", "answer": "Photosynthesis"},
            {"question": "The center of an atom.", "answer": "Nucleus"},
            {"question": "This gas do humans breathe in.", "answer": "Oxygen"},
            {"question": "The force that pulls objects toward Earth.", "answer": "Gravity"},
        ],
    },
    {
        "category": "Pop Culture",
        "clues": [
            {"question": "The movie with toys that come to life.", "answer": "Toy Story"},
            {"question": "The wizard school in Harry Potter.", "answer": "Hogwarts"},
            {"question": "Famous animated mouse mascot.", "answer": "Mickey Mouse"},
            {"question": "The superhero known as the Dark Knight.", "answer": "Batman"},
            {"question": "Band that sang 'Hey Jude'.", "answer": "The Beatles"},
        ],
    },
    {
        "category": "Sports",
        "clues": [
            {"question": "Sport played on ice with a puck.", "answer": "Hockey"},
            {"question": "Number of points for a touchdown.", "answer": "6"},
            {"question": "Country that hosts the Tour de France.", "answer": "France"},
            {"question": "The NBA team from Los Angeles with purple and gold.", "answer": "Lakers"},
            {"question": "Sport with rackets and a net, played in sets.", "answer": "Tennis"},
        ],
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

QUICKDRAW_PROMPTS: List[str] = [
    "Name a snack you'd bring on a road trip.",
    "One-word answer: a superpower you'd pick.",
    "Name something you'd buy if you won $50 today.",
    "One-word answer: a movie genre.",
    "Name a song that always gets stuck in your head.",
    "One-word answer: a pet name.",
    "Name a food you could eat every day.",
    "One-word answer: a famous place.",
    "Name something you always forget to charge.",
    "One-word answer: a board game.",
    "Name a color that fits a Monday morning.",
    "One-word answer: a sport.",
    "Name a hobby you'd try for a week.",
    "One-word answer: a dessert.",
    "Name a cartoon character.",
    "One-word answer: a drink.",
    "Name something found in a junk drawer.",
    "One-word answer: a word that sounds funny.",
    "Name a small luxury.",
    "One-word answer: a party theme.",
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

VOTEBATTLE_PROMPTS: List[str] = [
    "Best excuse for being late.",
    "Worst pickup line.",
    "Most dramatic way to say hello.",
    "If your outfit had a slogan, what is it?",
    "Worst thing to say on a first date.",
    "Describe Monday in three words.",
    "Best name for a group chat.",
    "Most suspicious thing to find in a fridge.",
    "The elevator opens to... what?",
    "A terrible name for a pet.",
    "What is the most awkward superpower?",
    "Your phone autocorrects everything to...",
    "The world's shortest horror story.",
    "A slogan for a very sleepy coffee shop.",
    "Worst theme for a birthday party.",
    "What is the least inspiring motto?",
    "A terrible name for a band.",
    "What would you rename the internet?",
    "The most honest fortune cookie message.",
    "The worst thing to put on a bumper sticker.",
]

SPYFALL_LOCATIONS: List[Dict[str, Any]] = [
    {
        "location": "Space Station",
        "roles": ["Commander", "Engineer", "Pilot", "Scientist", "Medic", "Tourist"],
    },
    {
        "location": "Movie Theater",
        "roles": ["Usher", "Projectionist", "Film Critic", "Snacker", "Director", "Ticket Taker"],
    },
    {
        "location": "Cruise Ship",
        "roles": ["Captain", "Chef", "Entertainer", "Navigator", "Lifeguard", "Passenger"],
    },
    {
        "location": "Hospital",
        "roles": ["Doctor", "Nurse", "Surgeon", "Patient", "Pharmacist", "Receptionist"],
    },
    {
        "location": "Museum",
        "roles": ["Curator", "Guard", "Artist", "Visitor", "Restorer", "Guide"],
    },
    {
        "location": "Wild West Town",
        "roles": ["Sheriff", "Outlaw", "Bartender", "Blacksmith", "Mayor", "Cowhand"],
    },
    {
        "location": "Beach",
        "roles": ["Lifeguard", "Surfer", "Vendor", "Tourist", "Photographer", "Camper"],
    },
    {
        "location": "Luxury Hotel",
        "roles": ["Concierge", "Chef", "Guest", "Manager", "Housekeeper", "Bellhop"],
    },
    {
        "location": "Carnival",
        "roles": ["Ringmaster", "Magician", "Ride Operator", "Clown", "Vendor", "Visitor"],
    },
    {
        "location": "High School",
        "roles": ["Teacher", "Principal", "Student", "Coach", "Nurse", "Janitor"],
    },
]

STATE: Dict[str, Any] = {
    "players": {},
    "scores": {},
    "teams_enabled": False,
    "team_count": 2,
    "teams": {},
    "team_names": {1: "Team 1", 2: "Team 2", 3: "Team 3", 4: "Team 4"},
    "mode": "mlt",
    "phase": "lobby",
    "round_id": 0,
    "prompt": "",
    "options": [],
    "correct_index": None,
    "prompt_bags": {},
    "prompt_last": {},
    "trivia_buzzer_phase": None,
    "trivia_buzzer_question": "",
    "trivia_buzzer_options": [],
    "trivia_buzzer_correct_index": None,
    "buzz_winner_pid": None,
    "buzz_winner_team_id": None,
    "buzz_ts": None,
    "answer_pid": None,
    "answer_team_id": None,
    "answer_choice": None,
    "steal_attempts": {},
    "trivia_buzzer_result": None,
    "trivia_buzzer_steal_enabled": True,
    "jeopardy_board": [],
    "jeopardy_phase": None,
    "jeopardy_selected": None,
    "jeopardy_buzz_team_id": None,
    "jeopardy_buzz_pid": None,
    "jeopardy_answer_team_id": None,
    "jeopardy_answer_pid": None,
    "jeopardy_answer_text": "",
    "jeopardy_steal_team_id": None,
    "jeopardy_pending_steal": False,
    "jeopardy_last_result": None,
    "jeopardy_timer_kind": None,
    "jeopardy_steal_enabled": True,
    "relay_phase": None,
    "relay_captains": {},
    "relay_question": {},
    "relay_answers": {},
    "draft_phase": None,
    "draft_pool": [],
    "draft_turn_order": [],
    "draft_turn_idx": 0,
    "draft_pick_team_id": None,
    "draft_picks": {},
    "draft_answer_order": [],
    "draft_answer_idx": 0,
    "draft_active_team_id": None,
    "draft_answer_choice": None,
    "draft_answer_pid": None,
    "draft_steal_choices": {},
    "draft_results": [],
    "wager_phase": None,
    "wager_amounts": {},
    "wager_answers": {},
    "wager_question": {},
    "estimate_prompt": "",
    "estimate_target": None,
    "estimate_phase": None,
    "estimate_submissions": {},
    "wavelength_target": None,
    "submissions": {},
    "submissions_locked": False,
    "votebattle_phase": None,
    "votebattle_entries": {},
    "votebattle_votes": {},
    "votebattle_order": [],
    "votebattle_counter": 0,
    "spyfall_phase": None,
    "spyfall_location": "",
    "spyfall_spy_pid": None,
    "spyfall_roles": {},
    "spyfall_auto_start_vote_on_timer": True,
    "spyfall_allow_self_vote": False,
    "mafia_phase": None,
    "mafia_roles": {},
    "mafia_alive": [],
    "mafia_wolf_votes": {},
    "mafia_day_votes": {},
    "mafia_seer_results": {},
    "mafia_last_eliminated": None,
    "mafia_seer_enabled": True,
    "mafia_auto_wolf_count": True,
    "mafia_wolf_count": 1,
    "mafia_reveal_roles_on_end": True,
    "last_result": None,
    "history": [],
    "host_message": "",
    "lobby_code": "",
    "require_lobby_code": True,
    "reclaim_requests": [],
    "reclaim_notices": {},
    "filter_mode": "mild",
    "openai_moderation_enabled": False,
    "timer_enabled": False,
    "timer_seconds": TIMER_DEFAULT_SECONDS,
    "vote_timer_seconds": VOTE_TIMER_DEFAULT_SECONDS,
    "auto_advance": True,
    "late_submit_policy": "lock_after_timer",
    "round_start_ts": None,
    "timer_start_ts": None,
    "timer_duration": None,
    "timer_expired": False,
    "wyr_points_majority": False,
    "quickdraw_scoring": "unique",
    "draft_question_count": 3,
    "estimate_price_is_right": False,
    "wager_max": 3,
    "wager_floor_zero": True,
    "prompt_mode": "random",
    "manual_prompt_text": "",
    "manual_wyr_a": "",
    "manual_wyr_b": "",
    "manual_trivia_0": "",
    "manual_trivia_1": "",
    "manual_trivia_2": "",
    "manual_trivia_3": "",
    "manual_correct_index": None,
    "manual_wavelength_target": None,
    "manual_wavelength_target_enabled": False,
    "manual_estimate_target": None,
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
        --bg: #0f1022;
        --bg-2: #1a1133;
        --card: #191c2b;
        --card-2: #222636;
        --accent: #ff7a59;
        --accent-2: #36d6c2;
        --text: #f8f5ff;
        --muted: #b9c4d6;
        --border: #2a2f44;
        --good: #32d488;
        --bad: #ff6b6b;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        color: var(--text);
        background: radial-gradient(1200px 600px at 10% -10%, #2a1b4b 0%, #0f1022 60%);
        font-family: "Trebuchet MS", "Verdana", sans-serif;
      }
      body.player {
        --card: #ffffff;
        --card-2: #fff6ec;
        --text: #1b1a23;
        --muted: #4c5868;
        --border: #e3d9cf;
        --accent: #ff7a59;
        --accent-2: #36b1d6;
        background: radial-gradient(1000px 600px at 20% -10%, #fff2d7 0%, #ffd7bd 60%);
      }
      body.host { font-size: 18px; }
      .wrap {
        max-width: 1020px;
        margin: 0 auto;
        padding: 24px;
      }
      .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: 0 18px 34px rgba(0, 0, 0, 0.18);
      }
      body.host .card { box-shadow: none; }
      .hero { padding: 26px; }
      h1, h2, h3 { margin: 0 0 12px 0; }
      .title { font-size: 2.2rem; font-weight: 800; }
      .subtitle { font-size: 1.05rem; }
      .muted { color: var(--muted); }
      .row { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
      .space-between { justify-content: space-between; }
      .stack { display: grid; gap: 12px; }
      .grid-2 { display: grid; gap: 16px; }
      .grid-3 { display: grid; gap: 16px; }
      @media (min-width: 920px) {
        .grid-2 { grid-template-columns: 1fr 1fr; }
        .grid-3 { grid-template-columns: 1fr 1fr 1fr; }
      }
      .btn {
        display: inline-flex;
        justify-content: center;
        align-items: center;
        gap: 8px;
        background: var(--accent);
        color: #ffffff;
        border: none;
        padding: 14px 18px;
        border-radius: 14px;
        font-size: 1rem;
        font-weight: 800;
        cursor: pointer;
        text-decoration: none;
        transition: transform 0.08s ease, box-shadow 0.08s ease;
        box-shadow: 0 10px 20px rgba(0, 0, 0, 0.18);
      }
      body.host .btn { font-size: 1.08rem; padding: 16px 20px; }
      .btn:hover { transform: translateY(-1px); }
      .btn.secondary { background: var(--accent-2); }
      .btn.ghost {
        background: transparent;
        color: var(--accent);
        border: 2px solid var(--accent);
        box-shadow: none;
      }
      .btn.full { width: 100%; }
      .input {
        width: 100%;
        padding: 12px 14px;
        border: 1px solid var(--border);
        border-radius: 12px;
        font-size: 1rem;
        background: var(--card-2);
        color: inherit;
      }
      .chip {
        display: inline-flex;
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.12);
        font-weight: 700;
        font-size: 0.85rem;
      }
      body.player .chip { background: rgba(0, 0, 0, 0.08); }
      .pill {
        display: inline-flex;
        padding: 6px 12px;
        border-radius: 999px;
        background: var(--accent);
        color: #fff;
        font-weight: 700;
        font-size: 0.85rem;
      }
      .pill.good { background: var(--good); }
      .pill.bad { background: var(--bad); }
      .alert {
        padding: 10px 12px;
        border-radius: 12px;
        background: rgba(255, 122, 89, 0.18);
        color: inherit;
        margin-bottom: 10px;
        border: 1px solid rgba(255, 122, 89, 0.4);
      }
      .timer {
        font-size: 1.4rem;
        font-weight: 800;
        padding: 6px 12px;
        border-radius: 12px;
        background: rgba(54, 214, 194, 0.2);
      }
      .code-box {
        font-size: 2rem;
        font-weight: 900;
        letter-spacing: 0.2rem;
        padding: 12px 16px;
        border-radius: 16px;
        background: var(--card-2);
        border: 1px dashed var(--border);
        display: inline-block;
      }
      .progress {
        width: 100%;
        height: 12px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 999px;
        overflow: hidden;
      }
      body.player .progress { background: rgba(0, 0, 0, 0.08); }
      .progress-fill {
        height: 100%;
        background: linear-gradient(90deg, var(--accent), var(--accent-2));
        width: 0%;
      }
      .list {
        display: grid;
        gap: 10px;
      }
      .list-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 12px;
        border-radius: 12px;
        background: var(--card-2);
        border: 1px solid var(--border);
      }
      .right { text-align: right; }
      img { max-width: 240px; height: auto; }
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
<div class="card hero">
  <div class="title">{{ app_title }}</div>
  <p class="muted">Grab a name and jump in.</p>
  {% if error %}
  <div class="alert">{{ error }}</div>
  {% endif %}
  <form method="post" action="{{ url_for('join') }}" class="stack">
    <label class="muted">Display name</label>
    <input class="input" type="text" name="name" maxlength="{{ name_max_len }}" placeholder="Display name" required>
    <label class="muted">Lobby code</label>
    <input class="input" type="text" name="lobby_code" maxlength="8" placeholder="ABCDE">
    {% if require_lobby_code %}
      <div class="muted">Ask the host for the lobby code.</div>
    {% else %}
      <div class="muted">Lobby code optional.</div>
    {% endif %}
    <button class="btn full" type="submit">Join the Party</button>
  </form>
</div>
<div class="card">
  <h2>Waiting Room</h2>
  <p class="muted">Keep this page open after joining.</p>
</div>
"""

NAME_CONFLICT_BODY = """
<div class="card hero">
  <div class="title">Name already taken</div>
  <p class="muted">Someone is already using "{{ name }}". Pick an option below.</p>
  <div class="stack">
    <form method="post" action="{{ url_for('join') }}">
      <input type="hidden" name="name" value="{{ name }}">
      <input type="hidden" name="lobby_code" value="{{ lobby_code }}">
      <input type="hidden" name="conflict_action" value="join_suffix">
      <button class="btn full" type="submit">Join as {{ suggested_name }}</button>
    </form>
    <form method="post" action="{{ url_for('join') }}">
      <input type="hidden" name="name" value="{{ name }}">
      <input type="hidden" name="lobby_code" value="{{ lobby_code }}">
      <input type="hidden" name="conflict_action" value="reclaim">
      <button class="btn secondary full" type="submit">Request reclaim {{ name }}</button>
    </form>
  </div>
</div>
"""

RECLAIM_WAIT_BODY = """
<div class="card hero">
  <div class="title">Reclaim request sent</div>
  <p class="muted">Waiting for the host to approve your name.</p>
  <div class="pill">Stay on this page</div>
</div>
<script>
  setTimeout(function () { window.location.reload(); }, 2500);
</script>
"""

PLAY_BODY = """
<div class="card hero">
  <div class="row space-between">
    <div>
      <div class="muted">You are</div>
      <div class="title">{{ player_name }}</div>
      {% if team_label %}
        <div class="pill">{{ team_label }}</div>
      {% endif %}
    </div>
    <div class="right">
      <div class="chip">{{ mode_label }}</div>
      <div class="muted">Round {{ round_id }}</div>
      {% if timer_remaining is not none %}
        <div class="timer">{{ timer_remaining }}s</div>
      {% endif %}
    </div>
  </div>
</div>

{% if message %}
<div class="card">
  <div class="alert">{{ message }}</div>
</div>
{% endif %}

<div class="card">
  <h2>Now Playing</h2>
  {% if mode == "spyfall" and spyfall_phase == "question" and is_spy %}
    <div class="title">Secret Location</div>
    <p class="muted">You are the spy. Figure out the location.</p>
  {% else %}
    <div class="title">{{ prompt or "Waiting..." }}</div>
  {% endif %}
  {% if submissions_locked %}
    <div class="pill bad">Submissions locked</div>
  {% endif %}
</div>

{% if phase == "lobby" %}
  <div class="card">
    <h2>Waiting for the host...</h2>
    <p class="muted">Round will start soon. Stay on this page.</p>
  </div>
{% elif phase == "in_round" %}
{% if submitted and mode not in ("trivia_buzzer", "team_trivia") %}
<div class="card">
  <div class="pill good">Submitted</div>
  <p class="muted">Waiting for others.</p>
</div>
{% endif %}

  {% if mode == "spyfall" %}
    <div class="card">
      <h2>Your Role</h2>
      {% if is_spy %}
        <div class="title">You are the SPY</div>
        <p class="muted">Blend in and figure out the location.</p>
      {% else %}
        <div class="title">{{ spyfall_location }}</div>
        <p class="muted">Role hint: {{ spyfall_role or "Guest" }}</p>
      {% endif %}
      {% if spyfall_phase == "vote" %}
        {% if not submitted %}
          <form method="post" action="{{ url_for('submit') }}" class="stack">
            <input type="hidden" name="round_id" value="{{ round_id }}">
            {% for p in player_choices %}
              {% if p.pid == pid %}
                <button class="btn ghost full" type="button" disabled>Your name</button>
              {% else %}
                <button class="btn full" type="submit" name="vote" value="{{ p.pid }}">{{ p.name }}</button>
              {% endif %}
            {% endfor %}
          </form>
        {% else %}
          <p class="muted">Vote received. Waiting for others.</p>
        {% endif %}
      {% else %}
        <p class="muted">Ask questions IRL. Host will start the vote.</p>
      {% endif %}
    </div>

  {% elif mode == "mafia" %}
    <div class="card">
      <h2>Your Role</h2>
      <div class="pill">{{ mafia_role or "villager" }}</div>
      {% if not mafia_alive %}
        <div class="pill bad">Eliminated</div>
      {% endif %}
      {% if seer_result %}
        <p class="muted">Last inspect: {{ seer_result.target_name }} is {{ "a Werewolf" if seer_result.is_werewolf else "not a Werewolf" }}.</p>
      {% endif %}
    </div>
    <div class="card">
      <h2>{{ "Night" if mafia_phase == "night" else "Day" }}</h2>
      {% if mafia_phase == "night" %}
        {% if mafia_role == "werewolf" and mafia_alive %}
          {% if not submitted %}
            <form method="post" action="{{ url_for('submit') }}" class="stack">
              <input type="hidden" name="round_id" value="{{ round_id }}">
              {% for p in alive_choices %}
                {% if p.pid == pid %}
                  <button class="btn ghost full" type="button" disabled>You</button>
                {% else %}
                  <button class="btn full" type="submit" name="wolf_target" value="{{ p.pid }}">{{ p.name }}</button>
                {% endif %}
              {% endfor %}
            </form>
          {% else %}
            <p class="muted">Target locked. Waiting for dawn...</p>
          {% endif %}
        {% elif mafia_role == "seer" and mafia_alive %}
          {% if not submitted %}
            <form method="post" action="{{ url_for('submit') }}" class="stack">
              <input type="hidden" name="round_id" value="{{ round_id }}">
              {% for p in alive_choices %}
                {% if p.pid == pid %}
                  <button class="btn ghost full" type="button" disabled>You</button>
                {% else %}
                  <button class="btn full" type="submit" name="seer_target" value="{{ p.pid }}">Inspect {{ p.name }}</button>
                {% endif %}
              {% endfor %}
            </form>
          {% else %}
            <p class="muted">Inspection sent. Waiting for dawn...</p>
          {% endif %}
        {% else %}
          <p class="muted">You are asleep. Waiting for dawn...</p>
        {% endif %}
      {% elif mafia_phase == "day" %}
        {% if mafia_last_eliminated %}
          <p class="muted">Last eliminated: {{ mafia_last_eliminated }}</p>
        {% endif %}
        {% if mafia_alive and not submitted %}
          <form method="post" action="{{ url_for('submit') }}" class="stack">
            <input type="hidden" name="round_id" value="{{ round_id }}">
            {% for p in alive_choices %}
              <button class="btn full" type="submit" name="vote" value="{{ p.pid }}">{{ p.name }}</button>
            {% endfor %}
          </form>
        {% else %}
          <p class="muted">Waiting for the village vote...</p>
        {% endif %}
      {% else %}
        <p class="muted">Game over. Waiting for the host.</p>
      {% endif %}
    </div>

  {% elif mode in ("trivia_buzzer", "team_trivia") %}
    <div class="card">
      <h2>Buzzer</h2>
      {% if buzz_winner_name %}
        <div class="pill">
          Buzz winner: {{ buzz_winner_name }}{% if buzz_winner_team_label %} ({{ buzz_winner_team_label }}){% endif %}
        </div>
      {% endif %}
      {% if trivia_buzzer_phase == "buzz" %}
        {% if buzz_winner_name %}
          <p class="muted">Waiting for the host to open answers.</p>
        {% elif can_buzz %}
          <form method="post" action="{{ url_for('submit') }}" class="stack">
            <input type="hidden" name="round_id" value="{{ round_id }}">
            <button class="btn full" type="submit" name="buzz" value="1">BUZZ</button>
          </form>
        {% else %}
          <p class="muted">Waiting for a buzz...</p>
        {% endif %}
      {% elif trivia_buzzer_phase == "answer" %}
        {% if answer_locked %}
          <div class="pill good">Answer locked</div>
          <p class="muted">Waiting for the host.</p>
        {% elif can_answer %}
          <form method="post" action="{{ url_for('submit') }}" class="stack">
            <input type="hidden" name="round_id" value="{{ round_id }}">
            {% for opt in options %}
              <button class="btn full" type="submit" name="choice" value="{{ loop.index0 }}">
                {{ option_labels[loop.index0] }}: {{ opt }}
              </button>
            {% endfor %}
          </form>
        {% else %}
          <p class="muted">Waiting for the answer...</p>
        {% endif %}
      {% elif trivia_buzzer_phase == "steal" %}
        {% if has_steal_attempt %}
          <div class="pill good">Steal submitted</div>
          <p class="muted">Waiting for the host.</p>
        {% elif can_steal %}
          <form method="post" action="{{ url_for('submit') }}" class="stack">
            <input type="hidden" name="round_id" value="{{ round_id }}">
            {% for opt in options %}
              <button class="btn full" type="submit" name="choice" value="{{ loop.index0 }}">
                {{ option_labels[loop.index0] }}: {{ opt }}
              </button>
            {% endfor %}
          </form>
        {% else %}
          <p class="muted">Waiting for steals...</p>
        {% endif %}
      {% else %}
        <p class="muted">Waiting for the host...</p>
      {% endif %}
    </div>

  {% else %}
    <div class="card">
      {% if not submitted %}
        <form method="post" action="{{ url_for('submit') }}" class="stack">
          <input type="hidden" name="round_id" value="{{ round_id }}">
          {% if mode == "mlt" %}
            {% for p in player_choices %}
              <button class="btn full" type="submit" name="vote" value="{{ p.pid }}">{{ p.name }}</button>
            {% endfor %}
          {% elif mode == "wyr" %}
            <button class="btn full" type="submit" name="choice" value="0">A: {{ options[0] }}</button>
            <button class="btn secondary full" type="submit" name="choice" value="1">B: {{ options[1] }}</button>
          {% elif mode == "trivia" %}
            {% for opt in options %}
              <button class="btn full" type="submit" name="choice" value="{{ loop.index0 }}">{{ opt }}</button>
            {% endfor %}
          {% elif mode == "hotseat" %}
            <label class="muted">Your answer (max {{ text_max_len }} chars)</label>
            <textarea class="input" name="text_answer" maxlength="{{ text_max_len }}" rows="3" required></textarea>
            <button class="btn full" type="submit">Submit</button>
          {% elif mode == "quickdraw" %}
            <label class="muted">Short answer (max {{ quickdraw_max_len }} chars)</label>
            <input class="input" type="text" name="text_answer" maxlength="{{ quickdraw_max_len }}" required>
            <button class="btn full" type="submit">Submit</button>
          {% elif mode == "wavelength" %}
            <label class="muted">Your guess (0 - 100)</label>
            <input class="input" type="number" name="wavelength_guess" min="0" max="100" required>
            <button class="btn full" type="submit">Submit Guess</button>
          {% elif mode == "votebattle" %}
            {% if votebattle_phase == "submit" %}
              <label class="muted">Your entry (max {{ votebattle_max_len }} chars)</label>
              <textarea class="input" name="votebattle_text" maxlength="{{ votebattle_max_len }}" rows="3" required></textarea>
              <button class="btn full" type="submit">Submit Entry</button>
            {% elif votebattle_phase == "vote" %}
              <div class="muted">Vote for your favorite entry</div>
              {% for choice in votebattle_choices %}
                {% if choice.is_self %}
                  <button class="btn ghost full" type="button" disabled>Your entry</button>
                {% else %}
                  <button class="btn full" type="submit" name="votebattle_vote" value="{{ choice.id }}">{{ choice.text }}</button>
                {% endif %}
              {% endfor %}
            {% else %}
              <p class="muted">Waiting for the host to start voting.</p>
            {% endif %}
          {% endif %}
        </form>
      {% endif %}
    </div>
  {% endif %}

{% elif phase == "revealed" %}
  <div class="card">
    <h2>Results</h2>
    {% if results and results.mode == "mlt" %}
      {% if results.winners %}
        <div class="pill good">Winner(s): {{ results.winners|join(", ") }}</div>
      {% else %}
        <p class="muted">No votes were submitted.</p>
      {% endif %}
      <div class="list">
        {% for row in results.tally_rows %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.votes }}</span></div>
        {% endfor %}
      </div>
    {% elif results and results.mode == "wyr" %}
      <div class="list">
        <div class="list-item"><span>A: {{ results.option_a }}</span><span class="pill">{{ results.tally_a }}</span></div>
        <div class="list-item"><span>B: {{ results.option_b }}</span><span class="pill">{{ results.tally_b }}</span></div>
      </div>
      {% if results.majority_label %}
        <div class="pill good">Majority: {{ results.majority_label }}</div>
      {% else %}
        <p class="muted">Tie vote.</p>
      {% endif %}
    {% elif results and results.mode == "trivia" %}
      <p class="muted">Correct answer: {{ results.correct_text }}</p>
      <div class="list">
        {% for row in results.option_rows %}
          <div class="list-item"><span>{{ row.label }}</span><span class="pill">{{ row.votes }}</span></div>
        {% endfor %}
      </div>
    {% elif results and results.mode in ("trivia_buzzer", "team_trivia") %}
      {% if results.buzz_name %}
        <p class="muted">Buzz winner: {{ results.buzz_name }}{% if results.buzz_team %} ({{ results.buzz_team }}){% endif %}</p>
      {% else %}
        <p class="muted">No buzz.</p>
      {% endif %}
      {% if results.answer_name %}
        <p class="muted">Answer: {{ results.answer_name }}{% if results.answer_team %} ({{ results.answer_team }}){% endif %}</p>
        {% if results.answer_label %}
          <div class="pill">{{ results.answer_label }}</div>
        {% endif %}
      {% endif %}
      <p class="muted">Correct answer: {{ results.correct_text }}</p>
      {% if results.steal_name %}
        <p class="muted">Steal: {{ results.steal_name }}{% if results.steal_team %} ({{ results.steal_team }}){% endif %}</p>
      {% endif %}
      {% if results.scoring_team %}
        <div class="pill good">Scoring team: {{ results.scoring_team }} (+{{ results.points }})</div>
      {% elif results.scoring_names %}
        <div class="pill good">Scored: {{ results.scoring_names|join(", ") }} (+{{ results.points }})</div>
      {% else %}
        <p class="muted">No points awarded.</p>
      {% endif %}
    {% elif results and results.mode == "hotseat" %}
      <div class="list">
        {% for row in results.answers %}
          <div class="list-item"><span>{{ row.name }}</span><span>{{ row.answer }}</span></div>
        {% endfor %}
      </div>
    {% elif results and results.mode == "quickdraw" %}
      <div class="list">
        {% for row in results.answer_groups %}
          <div class="list-item">
            <span>{{ row.answer }} ({{ row.players|join(", ") }})</span>
            <span class="pill">{{ row.count }}</span>
          </div>
        {% endfor %}
      </div>
    {% elif results and results.mode == "wavelength" %}
      <p class="muted">Target: {{ results.target }}</p>
      {% if results.winners %}
        <div class="pill good">Closest: {{ results.winners|join(", ") }}</div>
      {% endif %}
      <div class="list">
        {% for row in results.guesses %}
          <div class="list-item"><span>{{ row.name }} - {{ row.guess }}</span><span class="pill">{{ row.distance }}</span></div>
        {% endfor %}
      </div>
    {% elif results and results.mode == "votebattle" %}
      {% if results.winners %}
        <div class="pill good">Winner(s): {{ results.winners|join(", ") }}</div>
      {% else %}
        <p class="muted">No votes submitted.</p>
      {% endif %}
      <div class="list">
        {% for row in results.entries %}
          <div class="list-item"><span>{{ row.text }}</span><span class="pill">{{ row.votes }}</span></div>
        {% endfor %}
      </div>
    {% elif results and results.mode == "spyfall" %}
      <div class="pill {{ 'good' if results.spy_caught else 'bad' }}">
        Spy {{ "caught" if results.spy_caught else "escaped" }}: {{ results.spy_name }}
      </div>
      <p class="muted">Location: {{ results.location }}</p>
      <div class="list">
        {% for row in results.tally_rows %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.votes }}</span></div>
        {% endfor %}
      </div>
    {% elif results and results.mode == "mafia" %}
      <div class="pill {{ 'good' if results.winner == 'villagers' else 'bad' }}">
        Winner: {{ results.winner or "unknown" }}
      </div>
      <div class="list">
        {% for row in results.roles %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.role }}</span></div>
        {% endfor %}
      </div>
    {% endif %}
  </div>
{% endif %}

{% if phase == "revealed" %}
  <div class="card">
    <h2>Scoreboard</h2>
    {% if scoreboard %}
      <div class="list">
        {% for row in scoreboard %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.score }}</span></div>
        {% endfor %}
      </div>
    {% else %}
      <p class="muted">No players yet.</p>
    {% endif %}
  </div>
  {% if team_scoreboard %}
    <div class="card">
      <h2>Team Scores</h2>
      <div class="list">
        {% for row in team_scoreboard %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.score }}</span></div>
        {% endfor %}
      </div>
    </div>
  {% endif %}
{% endif %}

<script>
  (function () {
    const initial = {
      phase: "{{ public_phase }}",
      mode: "{{ public_mode }}",
      roundId: {{ public_round_id }},
      votebattle: "{{ public_votebattle_phase or '' }}",
      spyfall: "{{ public_spyfall_phase or '' }}",
      mafia: "{{ public_mafia_phase or '' }}",
      triviaBuzzer: "{{ public_trivia_buzzer_phase or '' }}"
    };
    function isTextInput(el) {
      if (!el) { return false; }
      if (el.tagName === "TEXTAREA") { return true; }
      if (el.tagName !== "INPUT") { return false; }
      const type = (el.getAttribute("type") || "text").toLowerCase();
      return type === "text" || type === "search";
    }
    function hasFocusedInput() {
      return isTextInput(document.activeElement);
    }
    function hasDraftText() {
      const fields = document.querySelectorAll("textarea, input[type='text'], input[type='search']");
      for (let i = 0; i < fields.length; i += 1) {
        if ((fields[i].value || "").trim() !== "") {
          return true;
        }
      }
      return false;
    }
    async function poll() {
      try {
        const res = await fetch("{{ url_for('api_public_state') }}", { cache: "no-store" });
        if (!res.ok) { return; }
        const data = await res.json();
        const changed = (
          data.phase !== initial.phase ||
          data.mode !== initial.mode ||
          data.round_id !== initial.roundId ||
          (data.votebattle_phase || "") !== initial.votebattle ||
          (data.spyfall_phase || "") !== initial.spyfall ||
          (data.mafia_phase || "") !== initial.mafia ||
          (data.trivia_buzzer_phase || "") !== initial.triviaBuzzer
        );
        if (changed && !hasFocusedInput() && !hasDraftText()) {
          window.location.reload();
        }
      } catch (err) {
        return;
      }
    }
    setInterval(poll, {{ public_poll_ms }});
  })();
</script>
"""

HOST_BODY = """
<div class="card hero">
  <div class="row space-between">
    <div>
      <div class="title">MASTER SCREEN</div>
      <p class="muted">Players <span id="player-count">{{ player_count }}</span> | Round <span id="round-id">{{ round_id }}</span></p>
    </div>
    {% if timer_enabled %}
    <div class="right">
      <div class="timer" id="timer-badge">{{ timer_remaining if timer_remaining is not none else "--" }}s</div>
      <div class="muted" id="lock-badge">{{ "Locked" if submissions_locked else "Open" }}</div>
    </div>
    {% endif %}
  </div>
  <div class="row">
    <div class="pill">Mode: <span id="mode-label">{{ mode_label }}</span></div>
    <div class="pill">Phase: <span id="phase-label">{{ phase_label }}</span></div>
    <div class="pill">Submissions: <span id="submission-count">{{ submission_count }}</span> / <span id="submission-target">{{ submission_target }}</span></div>
  </div>
  {% if host_message %}
  <div class="alert">{{ host_message }}</div>
  {% endif %}
</div>

<div class="grid-2">
  <div class="card">
    <h2>Join the Party</h2>
    <div class="stack">
      <div class="row">
        <input class="input" id="join-url" type="text" value="{{ join_url }}" readonly>
        <button class="btn ghost" type="button" onclick="copyText('join-url')">Copy Join URL</button>
      </div>
      <div class="row">
        <div>
          <div class="muted">Lobby Code</div>
          <div class="code-box" id="lobby-code">{{ lobby_code }}</div>
        </div>
        <button class="btn secondary" type="button" onclick="copyText('lobby-code')">Copy Lobby Code</button>
      </div>
    </div>
    <p class="muted">Host URL (localhost): <span class="chip">{{ host_url }}</span></p>
    {% if join_qr_data %}
      <div style="margin-top:12px;">
        <img src="{{ join_qr_data }}" alt="Join QR">
      </div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Now Playing</h2>
    <div class="title" id="prompt-text">{{ prompt or "None" }}</div>
    {% if mode == "wavelength" %}
      <p class="muted">Target: <span id="wavelength-target">{{ wavelength_target }}</span></p>
    {% endif %}
    {% if mode == "votebattle" %}
      <p class="muted">Vote Battle phase: <span id="votebattle-phase">{{ votebattle_phase or "submit" }}</span></p>
      <p class="muted">Entries: <span id="votebattle-submit-count">{{ votebattle_submit_count }}</span> | Votes: <span id="votebattle-vote-count">{{ votebattle_vote_count }}</span></p>
    {% endif %}
    {% if mode in ("trivia_buzzer", "team_trivia") %}
      <p class="muted">Buzzer phase: <span id="trivia-buzzer-phase">{{ trivia_buzzer_phase or "buzz" }}</span></p>
      <p class="muted">Buzz winner: <span id="buzz-winner">{{ buzz_winner_display }}</span></p>
      <p class="muted">Answer by: <span id="answer-by">{{ answer_display }}</span></p>
    {% endif %}
    {% if mode == "spyfall" %}
      <p class="muted">Spyfall phase: <span id="spyfall-phase">{{ spyfall_phase or "question" }}</span></p>
    {% endif %}
    {% if mode == "mafia" %}
      <p class="muted">Mafia phase: <span id="mafia-phase">{{ mafia_phase or "night" }}</span></p>
    {% endif %}
    <div class="progress" style="margin-top:12px;">
      <div class="progress-fill" id="progress-fill" style="width: {{ progress_percent }}%;"></div>
    </div>
    <p class="muted" id="submission-names">
      {% if submission_names %}
        {{ submission_names|join(", ") }}
      {% else %}
        No submissions yet.
      {% endif %}
    </p>
  </div>
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
      <button class="btn ghost full" type="submit">Set Mode</button>
    </form>

    <form method="post" action="{{ url_for('host_action') }}" class="stack" style="margin-top:12px;">
      <button class="btn full" name="action" value="start_round" type="submit">Start Round</button>
      {% if show_progress_button %}
      <button class="btn secondary full" name="action" value="progress" type="submit" id="progress-btn">{{ progress_label }}</button>
      {% else %}
      <button class="btn secondary full" name="action" value="progress" type="submit" id="progress-btn" style="display: none;">Progress</button>
      {% endif %}
      {% if show_reveal_button %}
      <button class="btn secondary full" name="action" value="reveal" type="submit">Reveal Results</button>
      {% endif %}
      <button class="btn full" name="action" value="next_round" type="submit">Next Round</button>
      <button class="btn ghost full" name="action" value="reset_round" type="submit">Reset Round</button>
      <button class="btn ghost full" name="action" value="reset_scores" type="submit">Reset Scores</button>
      <button class="btn ghost full" name="action" value="download_recap" type="submit">Download Recap</button>
    </form>
  </div>

  {% if show_prompt_control %}
  <div class="card">
    <h2>Prompt Control</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="apply_prompt_settings">
      <label class="muted" for="prompt_mode">Prompt Mode</label>
      <select class="input" name="prompt_mode" id="prompt_mode">
        <option value="random" {% if prompt_mode != "manual" %}selected{% endif %}>Random</option>
        <option value="manual" {% if prompt_mode == "manual" %}selected{% endif %}>Manual</option>
      </select>
      <label class="muted" for="manual_prompt_text">
        {% if mode == "spyfall" %}Location{% else %}Prompt text{% endif %}
      </label>
      <textarea class="input" name="manual_prompt_text" id="manual_prompt_text" rows="2">{{ manual_prompt_text }}</textarea>

      {% if mode == "wyr" %}
        <label class="muted">Option A</label>
        <input class="input" type="text" name="manual_wyr_a" value="{{ manual_wyr_a }}">
        <label class="muted">Option B</label>
        <input class="input" type="text" name="manual_wyr_b" value="{{ manual_wyr_b }}">
      {% elif mode in ("trivia", "trivia_buzzer", "team_trivia") %}
        <label class="muted">Option 1</label>
        <input class="input" type="text" name="manual_trivia_0" value="{{ manual_trivia_0 }}">
        <label class="muted">Option 2</label>
        <input class="input" type="text" name="manual_trivia_1" value="{{ manual_trivia_1 }}">
        <label class="muted">Option 3</label>
        <input class="input" type="text" name="manual_trivia_2" value="{{ manual_trivia_2 }}">
        <label class="muted">Option 4</label>
        <input class="input" type="text" name="manual_trivia_3" value="{{ manual_trivia_3 }}">
        <label class="muted">Correct index (0-3)</label>
        <input class="input" type="number" name="manual_correct_index" min="0" max="3" value="{{ manual_correct_index if manual_correct_index is not none else '' }}">
      {% elif mode == "wavelength" %}
        <label class="muted">Manual target (optional)</label>
        <label>
          <input type="checkbox" name="manual_wavelength_target_enabled" {% if manual_wavelength_target_enabled %}checked{% endif %}>
          Use manual target
        </label>
        <input class="input" type="number" name="manual_wavelength_target" min="0" max="100" value="{{ manual_wavelength_target if manual_wavelength_target is not none else '' }}">
      {% endif %}
      <button class="btn ghost full" type="submit">Apply Prompt Settings</button>
    </form>
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="pick_random_prompt">
      <button class="btn full" type="submit">Pick Random Now</button>
    </form>
    <p class="muted">Manual mode applies to the next round.</p>
  </div>
  {% endif %}

  {% if show_game_settings_wyr or show_game_settings_quickdraw %}
  <div class="card">
    <h2>Game Settings</h2>
    {% if show_game_settings_wyr %}
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="set_wyr_points">
      <label>
        <input type="checkbox" name="points_majority" {% if wyr_points_majority %}checked{% endif %}>
        Award points to majority vote (WYR)
      </label>
      <button class="btn ghost full" type="submit">Update WYR Scoring</button>
    </form>
    {% endif %}
    {% if show_game_settings_quickdraw %}
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="set_quickdraw_scoring">
      <label class="muted">Quick Draw scoring</label>
      <label>
        <input type="radio" name="quickdraw_scoring" value="unique" {% if quickdraw_scoring == "unique" %}checked{% endif %}>
        Unique answers +1
      </label>
      <label>
        <input type="radio" name="quickdraw_scoring" value="host" {% if quickdraw_scoring == "host" %}checked{% endif %}>
        Host picks winner +1
      </label>
      <button class="btn ghost full" type="submit">Update Quick Draw Scoring</button>
    </form>
    {% endif %}
  </div>
  {% endif %}

  {% if show_game_settings_buzzer %}
  <div class="card">
    <h2>Buzzer Settings</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_trivia_buzzer_settings">
      <label>
        <input type="checkbox" name="steal_enabled" {% if trivia_buzzer_steal_enabled %}checked{% endif %}>
        Enable steals
      </label>
      <button class="btn ghost full" type="submit">Save Buzzer Settings</button>
    </form>
  </div>
  {% endif %}

  {% if show_game_settings_spyfall %}
  <div class="card">
    <h2>Spyfall Settings</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_spyfall_settings">
      <label>
        <input type="checkbox" name="auto_start_vote_on_timer" {% if spyfall_auto_start_vote_on_timer %}checked{% endif %}>
        Auto-start vote on timer
      </label>
      <label>
        <input type="checkbox" name="allow_self_vote" {% if spyfall_allow_self_vote %}checked{% endif %}>
        Allow self-vote
      </label>
      <button class="btn ghost full" type="submit">Save Spyfall Settings</button>
    </form>
  </div>
  {% endif %}

  {% if show_game_settings_mafia %}
  <div class="card">
    <h2>Mafia/Werewolf Settings</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_mafia_settings">
      <label>
        <input type="checkbox" name="seer_enabled" {% if mafia_seer_enabled %}checked{% endif %}>
        Enable seer (4+ players)
      </label>
      <label>
        <input type="checkbox" name="auto_wolf_count" {% if mafia_auto_wolf_count %}checked{% endif %}>
        Auto wolf count (1-2 based on players)
      </label>
      {% if not mafia_auto_wolf_count %}
        <label class="muted">Wolf count (1-2)</label>
        <input class="input" type="number" name="wolf_count" min="1" max="2" value="{{ mafia_wolf_count }}">
      {% endif %}
      <label>
        <input type="checkbox" name="reveal_roles_on_end" {% if mafia_reveal_roles_on_end %}checked{% endif %}>
        Reveal roles on end
      </label>
      <button class="btn ghost full" type="submit">Save Mafia Settings</button>
    </form>
  </div>
  {% endif %}
</div>

<div class="grid-2">
  <div class="card">
    <h2>Timer + Lobby</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_timer_settings">
      <label>
        <input type="checkbox" name="timer_enabled" {% if timer_enabled %}checked{% endif %}>
        Enable round timer
      </label>
      <label class="muted">Round timer seconds</label>
      <input class="input" type="number" name="timer_seconds" min="10" max="180" value="{{ timer_seconds }}">
      <label class="muted">Vote timer seconds</label>
      <input class="input" type="number" name="vote_timer_seconds" min="10" max="120" value="{{ vote_timer_seconds }}">
      <label>
        <input type="checkbox" name="auto_advance" {% if auto_advance %}checked{% endif %}>
        Auto-advance when timer ends
      </label>
      <label class="muted">Late submit policy</label>
      <select class="input" name="late_submit_policy">
        <option value="accept" {% if late_submit_policy == "accept" %}selected{% endif %}>Accept late submissions</option>
        <option value="lock_after_timer" {% if late_submit_policy != "accept" %}selected{% endif %}>Lock after timer</option>
      </select>
      <button class="btn ghost full" type="submit">Save Timer Settings</button>
    </form>
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="toggle_lobby_lock">
      <button class="btn ghost full" type="submit">
        {% if lobby_locked %}Unlock Lobby{% else %}Lock Lobby{% endif %}
      </button>
    </form>
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="toggle_allow_renames">
      <button class="btn ghost full" type="submit">
        {% if allow_renames %}Disable Renames{% else %}Enable Renames{% endif %}
      </button>
    </form>
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="toggle_lobby_code">
      <button class="btn ghost full" type="submit">
        {% if require_lobby_code %}Disable Lobby Code{% else %}Require Lobby Code{% endif %}
      </button>
    </form>
  </div>

  <div class="card">
    <h2>Teams</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_teams">
      <label>
        <input type="checkbox" name="teams_enabled" {% if teams_enabled %}checked{% endif %}>
        Enable teams
      </label>
      <label class="muted">Team count (2-4)</label>
      <input class="input" type="number" name="team_count" min="2" max="4" value="{{ team_count }}">
      {% for team_id in range(1, team_count + 1) %}
        <label class="muted">Team {{ team_id }} name</label>
        <input class="input" type="text" name="team_name_{{ team_id }}" value="{{ team_names.get(team_id, 'Team ' ~ team_id) }}">
      {% endfor %}
      <button class="btn ghost full" type="submit">Save Teams</button>
    </form>
    <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
      <input type="hidden" name="action" value="randomize_teams">
      <button class="btn secondary full" type="submit">Randomize Teams</button>
    </form>
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <h2>Safety Filter</h2>
    <form method="post" action="{{ url_for('host_action') }}" class="stack">
      <input type="hidden" name="action" value="set_filter_mode">
      <label class="muted">Profanity filter</label>
      <select class="input" name="filter_mode">
        <option value="off" {% if filter_mode == "off" %}selected{% endif %}>Off</option>
        <option value="mild" {% if filter_mode == "mild" %}selected{% endif %}>Mild</option>
        <option value="strict" {% if filter_mode == "strict" %}selected{% endif %}>Strict</option>
      </select>
      <label>
        <input type="checkbox" name="openai_moderation_enabled" {% if openai_moderation_enabled %}checked{% endif %} {% if not openai_enabled %}disabled{% endif %}>
        Enable OpenAI moderation (optional)
      </label>
      {% if not openai_enabled %}
        <div class="muted">OpenAI not configured.</div>
      {% endif %}
      <button class="btn ghost full" type="submit">Save Safety</button>
    </form>
  </div>

  <div class="card">
    <h2>Players</h2>
    {% if players %}
      <div class="list">
        {% for p in players %}
          <div class="list-item">
            <span>{{ p.name }}{% if p.team %} ({{ p.team }}){% endif %}</span>
            <form method="post" action="{{ url_for('host_action') }}">
              <input type="hidden" name="action" value="kick">
              <input type="hidden" name="pid" value="{{ p.pid }}">
              <button class="btn ghost" type="submit">Kick</button>
            </form>
          </div>
        {% endfor %}
      </div>
      <form method="post" action="{{ url_for('host_action') }}" style="margin-top:12px;">
        <button class="btn ghost full" name="action" value="kick_all" type="submit">Kick All Players</button>
      </form>
    {% else %}
      <p class="muted">No players yet.</p>
    {% endif %}
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <h2>Scoreboard</h2>
    {% if scoreboard %}
      <div class="list">
        {% for row in scoreboard %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.score }}</span></div>
        {% endfor %}
      </div>
    {% else %}
      <p class="muted">No scores yet.</p>
    {% endif %}
  </div>
  <div class="card">
    <h2>Team Scores</h2>
    {% if team_scoreboard %}
      <div class="list">
        {% for row in team_scoreboard %}
          <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.score }}</span></div>
        {% endfor %}
      </div>
    {% else %}
      <p class="muted">Teams disabled.</p>
    {% endif %}
  </div>
</div>

{% if reclaim_requests %}
<div class="card">
  <h2>Reclaim Requests</h2>
  <div class="list">
    {% for req in reclaim_requests %}
      <div class="list-item">
        <span>{{ req.name }}</span>
        <div class="row">
          <form method="post" action="{{ url_for('host_action') }}">
            <input type="hidden" name="action" value="approve_reclaim">
            <input type="hidden" name="request_id" value="{{ req.request_id }}">
            <button class="btn secondary" type="submit">Approve</button>
          </form>
          <form method="post" action="{{ url_for('host_action') }}">
            <input type="hidden" name="action" value="deny_reclaim">
            <input type="hidden" name="request_id" value="{{ req.request_id }}">
            <button class="btn ghost" type="submit">Deny</button>
          </form>
        </div>
      </div>
    {% endfor %}
  </div>
</div>
{% endif %}

{% if results %}
<div class="card">
  <h2>Latest Results</h2>
  {% if results.mode == "mlt" %}
    {% if results.winners %}
      <div class="pill good">Winner(s): {{ results.winners|join(", ") }}</div>
    {% else %}
      <p class="muted">No votes were submitted.</p>
    {% endif %}
    <div class="list">
      {% for row in results.tally_rows %}
        <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.votes }}</span></div>
      {% endfor %}
    </div>
  {% elif results.mode == "wyr" %}
    <div class="list">
      <div class="list-item"><span>A: {{ results.option_a }}</span><span class="pill">{{ results.tally_a }}</span></div>
      <div class="list-item"><span>B: {{ results.option_b }}</span><span class="pill">{{ results.tally_b }}</span></div>
    </div>
    {% if results.majority_label %}
      <div class="pill good">Majority: {{ results.majority_label }}</div>
    {% else %}
      <p class="muted">Tie vote.</p>
    {% endif %}
  {% elif results.mode == "trivia" %}
    <p class="muted">Correct answer: {{ results.correct_text }}</p>
    <div class="list">
      {% for row in results.option_rows %}
        <div class="list-item"><span>{{ row.label }}</span><span class="pill">{{ row.votes }}</span></div>
      {% endfor %}
    </div>
  {% elif results.mode in ("trivia_buzzer", "team_trivia") %}
    {% if results.buzz_name %}
      <p class="muted">Buzz winner: {{ results.buzz_name }}{% if results.buzz_team %} ({{ results.buzz_team }}){% endif %}</p>
    {% else %}
      <p class="muted">No buzz.</p>
    {% endif %}
    {% if results.answer_name %}
      <p class="muted">Answer: {{ results.answer_name }}{% if results.answer_team %} ({{ results.answer_team }}){% endif %}</p>
      {% if results.answer_label %}
        <div class="pill">{{ results.answer_label }}</div>
      {% endif %}
    {% endif %}
    <p class="muted">Correct answer: {{ results.correct_text }}</p>
    {% if results.steal_name %}
      <p class="muted">Steal: {{ results.steal_name }}{% if results.steal_team %} ({{ results.steal_team }}){% endif %}</p>
    {% endif %}
    {% if results.scoring_team %}
      <div class="pill good">Scoring team: {{ results.scoring_team }} (+{{ results.points }})</div>
    {% elif results.scoring_names %}
      <div class="pill good">Scored: {{ results.scoring_names|join(", ") }} (+{{ results.points }})</div>
    {% else %}
      <p class="muted">No points awarded.</p>
    {% endif %}
  {% elif results.mode == "hotseat" %}
    <div class="list">
      {% for row in results.answers %}
        <div class="list-item">
          <span>{{ row.name }}: {{ row.answer }}</span>
          <form method="post" action="{{ url_for('host_action') }}">
            <input type="hidden" name="action" value="award_point">
            <input type="hidden" name="pid" value="{{ row.pid }}">
            <button class="btn ghost" type="submit">Award</button>
          </form>
        </div>
      {% endfor %}
    </div>
  {% elif results.mode == "quickdraw" %}
    <p class="muted">Scoring: {{ "Unique answers +1" if quickdraw_scoring == "unique" else "Host picks winner +1" }}</p>
    <div class="list">
      {% for row in results.answer_groups %}
        <div class="list-item"><span>{{ row.answer }} ({{ row.players|join(", ") }})</span><span class="pill">{{ row.count }}</span></div>
      {% endfor %}
    </div>
    {% if quickdraw_scoring == "host" %}
      <div class="list" style="margin-top:12px;">
        {% for row in results.entries %}
          <div class="list-item">
            <span>{{ row.name }}: {{ row.answer }}</span>
            <form method="post" action="{{ url_for('host_action') }}">
              <input type="hidden" name="action" value="award_quickdraw">
              <input type="hidden" name="pid" value="{{ row.pid }}">
              <button class="btn ghost" type="submit">Award</button>
            </form>
          </div>
        {% endfor %}
      </div>
    {% endif %}
  {% elif results.mode == "wavelength" %}
    <p class="muted">Target: {{ results.target }}</p>
    {% if results.winners %}
      <div class="pill good">Closest: {{ results.winners|join(", ") }}</div>
    {% endif %}
    <div class="list">
      {% for row in results.guesses %}
        <div class="list-item"><span>{{ row.name }} - {{ row.guess }}</span><span class="pill">{{ row.distance }}</span></div>
      {% endfor %}
    </div>
  {% elif results.mode == "votebattle" %}
    {% if results.winners %}
      <div class="pill good">Winner(s): {{ results.winners|join(", ") }}</div>
    {% else %}
      <p class="muted">No votes submitted.</p>
    {% endif %}
    <div class="list">
      {% for row in results.entries %}
        <div class="list-item"><span>{{ row.text }}</span><span class="pill">{{ row.votes }}</span><span class="muted">{{ row.author or "Hidden" }}</span></div>
      {% endfor %}
    </div>
  {% elif results.mode == "spyfall" %}
    <div class="pill {{ 'good' if results.spy_caught else 'bad' }}">Spy {{ "caught" if results.spy_caught else "escaped" }}: {{ results.spy_name }}</div>
    <p class="muted">Location: {{ results.location }}</p>
    <div class="list">
      {% for row in results.tally_rows %}
        <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.votes }}</span></div>
      {% endfor %}
    </div>
  {% elif results.mode == "mafia" %}
    <div class="pill {{ 'good' if results.winner == 'villagers' else 'bad' }}">Winner: {{ results.winner or "unknown" }}</div>
    <div class="list">
      {% for row in results.roles %}
        <div class="list-item"><span>{{ row.name }}</span><span class="pill">{{ row.role }}</span></div>
      {% endfor %}
    </div>
  {% endif %}
</div>
{% endif %}

{% if openai_enabled %}
<div class="card">
  <h2>AI Prompt Generation</h2>
  <p class="muted">Generates new prompt pools. Existing rounds are unchanged.</p>
  <form method="post" action="{{ url_for('host_action') }}" class="stack">
    <button class="btn ghost full" name="action" value="generate_mlt" type="submit">Generate MLT Prompts</button>
    <button class="btn ghost full" name="action" value="generate_wyr" type="submit">Generate WYR Prompts</button>
    <button class="btn ghost full" name="action" value="generate_trivia" type="submit">Generate Trivia Questions</button>
    <button class="btn ghost full" name="action" value="generate_hotseat" type="submit">Generate Hot Seat Prompts</button>
    <button class="btn ghost full" name="action" value="generate_quickdraw" type="submit">Generate Quick Draw Prompts</button>
    <button class="btn ghost full" name="action" value="generate_wavelength" type="submit">Generate Wavelength Prompts</button>
    <button class="btn ghost full" name="action" value="generate_votebattle" type="submit">Generate Vote Battle Prompts</button>
  </form>
</div>
{% endif %}

<script>
  function copyText(id) {
    const el = document.getElementById(id);
    if (!el) { return; }
    const text = el.value || el.textContent || "";
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text);
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand("copy");
    sel.removeAllRanges();
  }

  (function () {
    async function poll() {
      try {
        const res = await fetch("{{ url_for('api_state') }}", { cache: "no-store" });
        if (!res.ok) { return; }
        const data = await res.json();
        const playerCount = document.getElementById("player-count");
        const submissionCount = document.getElementById("submission-count");
        const submissionTarget = document.getElementById("submission-target");
        const modeLabel = document.getElementById("mode-label");
        const phaseLabel = document.getElementById("phase-label");
        const roundId = document.getElementById("round-id");
        const promptText = document.getElementById("prompt-text");
        const progressFill = document.getElementById("progress-fill");
        const votebattlePhase = document.getElementById("votebattle-phase");
        const votebattleSubmitCount = document.getElementById("votebattle-submit-count");
        const votebattleVoteCount = document.getElementById("votebattle-vote-count");
        const submissionNames = document.getElementById("submission-names");
        const spyfallPhase = document.getElementById("spyfall-phase");
        const mafiaPhase = document.getElementById("mafia-phase");
        const wavelengthTarget = document.getElementById("wavelength-target");
        const triviaBuzzerPhase = document.getElementById("trivia-buzzer-phase");
        const buzzWinner = document.getElementById("buzz-winner");
        const answerBy = document.getElementById("answer-by");
        const progressBtn = document.getElementById("progress-btn");
        if (playerCount) { playerCount.textContent = data.player_count; }
        if (submissionCount) { submissionCount.textContent = data.submission_count; }
        if (submissionTarget) { submissionTarget.textContent = data.submission_target; }
        if (modeLabel) { modeLabel.textContent = data.mode_label || data.mode; }
        if (phaseLabel) { phaseLabel.textContent = data.phase_label || data.phase; }
        if (roundId) { roundId.textContent = data.round_id; }
        if (promptText) { promptText.textContent = data.prompt || "None"; }
        if (progressFill) { progressFill.style.width = data.progress_percent + "%"; }
        if (wavelengthTarget && data.wavelength_target !== null && data.wavelength_target !== undefined) {
          wavelengthTarget.textContent = data.wavelength_target;
        }
        if (votebattlePhase) { votebattlePhase.textContent = data.votebattle_phase || "submit"; }
        if (votebattleSubmitCount) { votebattleSubmitCount.textContent = data.votebattle_submit_count || 0; }
        if (votebattleVoteCount) { votebattleVoteCount.textContent = data.votebattle_vote_count || 0; }
        if (spyfallPhase) { spyfallPhase.textContent = data.spyfall_phase || "question"; }
        if (mafiaPhase) { mafiaPhase.textContent = data.mafia_phase || "night"; }
        if (triviaBuzzerPhase) { triviaBuzzerPhase.textContent = data.trivia_buzzer_phase || "buzz"; }
        if (buzzWinner) { buzzWinner.textContent = data.buzz_winner_display || "--"; }
        if (answerBy) { answerBy.textContent = data.answer_display || "--"; }
        if (submissionNames && Array.isArray(data.submission_names)) {
          submissionNames.textContent = data.submission_names.length ? data.submission_names.join(", ") : "No submissions yet.";
        }
        if (progressBtn) {
          if (data.show_progress_button) {
            progressBtn.style.display = "";
            progressBtn.textContent = data.progress_label || "Progress";
          } else {
            progressBtn.style.display = "none";
          }
        }
      } catch (err) {
        return;
      }
    }
    poll();
    setInterval(poll, {{ host_poll_ms }});
  })();

  (function () {
    async function pollTimer() {
      try {
        const res = await fetch("{{ url_for('api_host_timer') }}", { cache: "no-store" });
        if (!res.ok) { return; }
        const data = await res.json();
        const timer = document.getElementById("timer-badge");
        const lockBadge = document.getElementById("lock-badge");
        if (timer && data.timer_remaining !== null && data.timer_remaining !== undefined) {
          timer.textContent = data.timer_remaining + "s";
        }
        if (lockBadge) {
          lockBadge.textContent = data.submissions_locked ? "Locked" : "Open";
        }
      } catch (err) {
        return;
      }
    }
    {% if timer_enabled %}
      pollTimer();
      setInterval(pollTimer, {{ host_timer_poll_ms }});
    {% endif %}
  })();
</script>
"""

HOST_LOCKED_BODY = """
<div class="card hero">
  <div class="title">MASTER SCREEN</div>
  <p class="muted">{{ lock_message }}</p>
  <p class="muted">Open this on the laptop: <span class="chip">{{ host_url }}</span></p>
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


def normalize_lobby_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def make_lobby_code(length: int = JOIN_CODE_LENGTH) -> str:
    return "".join(secrets.choice(LOBBY_CODE_CHARS) for _ in range(length))


def validate_lobby_code(input_code: str, expected_code: str, required: bool) -> bool:
    if not required:
        return True
    if not input_code:
        return False
    return normalize_lobby_code(input_code) == normalize_lobby_code(expected_code)


def contains_banned_word(text: str, mode: str) -> bool:
    if mode == "off":
        return False
    words = re.findall(r"[a-zA-Z]+", text.lower())
    banned = BANNED_WORDS_STRICT if mode == "strict" else BANNED_WORDS_MILD
    return any(word in banned for word in words)


def clean_text_answer(text: str, limit: int = TEXT_MAX_LEN) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:limit].strip()


def check_text_allowed(text: str, state: Dict[str, Any]) -> Optional[str]:
    filter_mode = state.get("filter_mode", "mild")
    if contains_banned_word(text, filter_mode):
        return "Keep it PG-13."
    if state.get("openai_moderation_enabled"):
        allowed, err = openai_moderate_text(text)
        if allowed is None:
            state["host_message"] = err or "OpenAI moderation failed."
            return "Moderation unavailable."
        if not allowed:
            return "Keep it PG-13."
    return None


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
    if mode == "votebattle":
        if state.get("votebattle_phase") == "vote":
            return len(state.get("votebattle_votes", {}))
        return len(state.get("votebattle_entries", {}))
    if mode in ("trivia_buzzer", "team_trivia"):
        phase = state.get("trivia_buzzer_phase")
        if phase == "buzz":
            return 1 if state.get("buzz_winner_pid") else 0
        if phase == "answer":
            return 1 if state.get("answer_choice") is not None else 0
        if phase == "steal":
            return len(state.get("steal_attempts", {}))
        return 0
    if mode == "mafia":
        if state.get("mafia_phase") == "night":
            return len(state.get("mafia_wolf_votes", {})) + len(state.get("mafia_seer_results", {}))
        if state.get("mafia_phase") == "day":
            return len(state.get("mafia_day_votes", {}))
        return 0
    return len(state.get("submissions", {}))


def get_active_submission_names(state: Dict[str, Any]) -> List[str]:
    players = state.get("players", {})
    mode = state.get("mode")
    if mode == "votebattle":
        if state.get("votebattle_phase") == "vote":
            pids = state.get("votebattle_votes", {}).keys()
        else:
            pids = state.get("votebattle_entries", {}).keys()
    elif mode in ("trivia_buzzer", "team_trivia"):
        phase = state.get("trivia_buzzer_phase")
        if phase == "buzz":
            buzz_pid = state.get("buzz_winner_pid")
            pids = [buzz_pid] if buzz_pid else []
        elif phase == "answer":
            answer_pid = state.get("answer_pid")
            pids = [answer_pid] if answer_pid else []
        elif phase == "steal":
            pids = state.get("steal_attempts", {}).keys()
        else:
            pids = []
    elif mode == "mafia":
        if state.get("mafia_phase") == "night":
            pids = list(state.get("mafia_wolf_votes", {}).keys()) + list(state.get("mafia_seer_results", {}).keys())
        elif state.get("mafia_phase") == "day":
            pids = state.get("mafia_day_votes", {}).keys()
        else:
            pids = []
    else:
        pids = state.get("submissions", {}).keys()
    names = [players.get(pid, {}).get("name", "Unknown") for pid in pids]
    names.sort(key=lambda name: name.lower())
    return names


def unique_answer_pids(submissions: Dict[str, Any]) -> List[str]:
    normalized_map: Dict[str, List[str]] = {}
    for pid, answer in submissions.items():
        normalized = normalize_text(str(answer))
        if not normalized:
            continue
        normalized_map.setdefault(normalized, []).append(pid)
    unique = []
    for pids in normalized_map.values():
        if len(pids) == 1:
            unique.append(pids[0])
    return unique


def build_tally(submissions: Dict[str, Any], valid_pids: List[str]) -> Dict[str, int]:
    tally = {pid: 0 for pid in valid_pids}
    for vote in submissions.values():
        if vote in tally:
            tally[vote] += 1
    return tally


def pick_winners_from_tally(tally: Dict[str, int]) -> Tuple[List[str], int]:
    if not tally:
        return [], 0
    max_votes = max(tally.values())
    winners = [pid for pid, votes in tally.items() if votes == max_votes and votes > 0]
    return winners, max_votes


def get_submission_target_count(state: Dict[str, Any]) -> int:
    players = state.get("players", {})
    mode = state.get("mode")
    if mode in ("trivia_buzzer", "team_trivia"):
        phase = state.get("trivia_buzzer_phase")
        if phase == "buzz":
            return 1
        if phase == "answer":
            return 1
        if phase == "steal":
            if mode == "team_trivia":
                team_id = state.get("buzz_winner_team_id")
                stealers = [
                    pid for pid, tid in state.get("teams", {}).items() if team_id is None or tid != team_id
                ]
                return max(1, len(stealers))
            if state.get("buzz_winner_pid"):
                return max(1, len(players) - 1)
            return max(1, len(players))
    if mode == "mafia":
        alive = set(state.get("mafia_alive", []))
        if state.get("mafia_phase") == "night":
            roles = state.get("mafia_roles", {})
            wolves = [pid for pid, role in roles.items() if role == "werewolf" and pid in alive]
            seers = [pid for pid, role in roles.items() if role == "seer" and pid in alive]
            return len(wolves) + len(seers)
        if state.get("mafia_phase") == "day":
            return len(alive)
        return 0
    if mode == "spyfall" and state.get("spyfall_phase") == "vote":
        return len(players)
    if mode == "votebattle" and state.get("votebattle_phase") == "vote":
        return len(players)
    return len(players)


def build_votebattle_choices(state: Dict[str, Any], pid: str) -> List[Dict[str, Any]]:
    choices = []
    order = state.get("votebattle_order", [])
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


def openai_moderate_text(text: str) -> Tuple[Optional[bool], Optional[str]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, "OpenAI moderation not configured."
    try:
        import openai  # type: ignore
    except Exception:
        return None, "openai package not installed."
    try:
        if hasattr(openai, "OpenAI"):
            client = openai.OpenAI(api_key=api_key)
            resp = client.moderations.create(model="omni-moderation-latest", input=text)
            flagged = bool(resp.results[0].flagged)
        else:
            openai.api_key = api_key
            resp = openai.Moderation.create(input=text)
            flagged = bool(resp["results"][0]["flagged"])
        return not flagged, None
    except Exception as exc:
        return None, f"OpenAI moderation failed: {exc}"


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


def generate_quickdraw_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 25 PG-13 Quick Draw prompts for short, one-line answers. "
        "Return a JSON array of strings. Keep prompts short and avoid player names."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 10:
        return None, "OpenAI returned too few quick draw prompts."
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


def generate_votebattle_prompts() -> Tuple[Optional[List[str]], Optional[str]]:
    prompt = (
        "Create 20 PG-13 Vote Battle prompts for short text entries. "
        "Return a JSON array of strings. Avoid player names and keep prompts punchy."
    )
    text, err = call_openai(prompt)
    if err:
        return None, err
    data = parse_json_from_text(text or "")
    if not isinstance(data, list):
        return None, "OpenAI response was not a JSON list."
    items = [str(item).strip() for item in data if str(item).strip()]
    if len(items) < 5:
        return None, "OpenAI returned too few vote battle prompts."
    return items, None


def get_state_snapshot() -> Dict[str, Any]:
    with STATE_LOCK:
        return copy.deepcopy(STATE)


def label_for_mode(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


def label_for_phase(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase)


PROGRESS_ACTION_LABELS = {
    "votebattle_start_vote": "Start Vote Battle Voting",
    "spyfall_start_vote": "Start Spy Vote",
    "mafia_start_day": "Resolve Night / Start Day",
    "mafia_resolve_day": "Resolve Day Vote",
    "mafia_end_game": "End Mafia Game",
    "buzzer_start_answer": "Start Answer",
    "buzzer_resolve_answer": "Resolve Answer",
    "jeopardy_start_answer": "Start Answer",
    "jeopardy_back_to_board": "Back to Board",
    "relay_reveal": "Lock Answers / Reveal",
    "draft_start_answers": "Start Answers",
    "draft_resolve_answer": "Resolve / Next Team",
    "draft_resolve_steal": "Resolve Steal",
    "wager_start_question": "Start Question",
    "wager_reveal": "Reveal / Score",
    "estimate_reveal": "Reveal Results",
    "reveal": "Reveal Results",
}


def resolve_progress_action(
    mode: str,
    phase: str,
    votebattle_phase: Optional[str] = None,
    spyfall_phase: Optional[str] = None,
    mafia_phase: Optional[str] = None,
    trivia_buzzer_phase: Optional[str] = None,
    jeopardy_phase: Optional[str] = None,
    relay_phase: Optional[str] = None,
    draft_phase: Optional[str] = None,
    wager_phase: Optional[str] = None,
    estimate_phase: Optional[str] = None,
) -> Optional[str]:
    if mode == "votebattle":
        if phase == "in_round" and votebattle_phase == "submit":
            return "votebattle_start_vote"
        if phase == "in_round" and votebattle_phase == "vote":
            return "reveal"
        return None
    if mode == "spyfall":
        if phase == "in_round" and spyfall_phase == "question":
            return "spyfall_start_vote"
        if phase == "in_round" and spyfall_phase == "vote":
            return "reveal"
        return None
    if mode == "mafia":
        if phase == "in_round" and mafia_phase == "night":
            return "mafia_start_day"
        if phase == "in_round" and mafia_phase == "day":
            return "mafia_resolve_day"
        if mafia_phase == "over" or phase == "revealed":
            return "mafia_end_game"
        return None
    if mode in ("trivia_buzzer", "team_trivia"):
        if phase != "in_round":
            return None
        if trivia_buzzer_phase == "buzz":
            return "buzzer_start_answer"
        if trivia_buzzer_phase == "answer":
            return "buzzer_resolve_answer"
        if trivia_buzzer_phase == "steal":
            return "reveal"
        return None
    if mode == "team_jeopardy":
        if phase != "in_round":
            return None
        if jeopardy_phase == "clue":
            return "jeopardy_start_answer"
        if jeopardy_phase == "reveal":
            return "jeopardy_back_to_board"
        return None
    if mode == "relay_trivia":
        if phase == "in_round" and relay_phase == "question":
            return "relay_reveal"
        return None
    if mode == "trivia_draft":
        if phase != "in_round":
            return None
        if draft_phase == "draft":
            return "draft_start_answers"
        if draft_phase == "answer":
            return "draft_resolve_answer"
        if draft_phase == "steal":
            return "draft_resolve_steal"
        return None
    if mode == "wager_trivia":
        if phase != "in_round":
            return None
        if wager_phase == "wager":
            return "wager_start_question"
        if wager_phase == "question":
            return "wager_reveal"
        return None
    if mode == "estimation_duel":
        if phase == "in_round" and estimate_phase == "submit":
            return "estimate_reveal"
        return None
    if phase == "in_round":
        return "reveal"
    return None


def get_progress_ui(
    mode: str,
    phase: str,
    votebattle_phase: Optional[str] = None,
    spyfall_phase: Optional[str] = None,
    mafia_phase: Optional[str] = None,
    trivia_buzzer_phase: Optional[str] = None,
    jeopardy_phase: Optional[str] = None,
    relay_phase: Optional[str] = None,
    draft_phase: Optional[str] = None,
    wager_phase: Optional[str] = None,
    estimate_phase: Optional[str] = None,
) -> Tuple[bool, str]:
    if mode not in (
        "votebattle",
        "spyfall",
        "mafia",
        "trivia_buzzer",
        "team_trivia",
        "team_jeopardy",
        "relay_trivia",
        "trivia_draft",
        "wager_trivia",
        "estimation_duel",
    ):
        return False, ""
    action = resolve_progress_action(
        mode,
        phase,
        votebattle_phase,
        spyfall_phase,
        mafia_phase,
        trivia_buzzer_phase,
        jeopardy_phase,
        relay_phase,
        draft_phase,
        wager_phase,
        estimate_phase,
    )
    if not action:
        return False, ""
    label = PROGRESS_ACTION_LABELS.get(action, "Progress")
    return True, label


def reset_timer_locked(state: Dict[str, Any], seconds: Optional[int]) -> None:
    if not state.get("timer_enabled"):
        state["timer_start_ts"] = None
        state["timer_duration"] = None
        state["timer_expired"] = False
        return
    duration = int(seconds or state.get("timer_seconds", TIMER_DEFAULT_SECONDS))
    state["timer_start_ts"] = time.time()
    state["timer_duration"] = max(1, duration)
    state["timer_expired"] = False


def stop_timer_locked(state: Dict[str, Any]) -> None:
    state["timer_start_ts"] = None
    state["timer_duration"] = None
    state["timer_expired"] = False


def get_timer_remaining(state: Dict[str, Any]) -> Optional[int]:
    if not state.get("timer_enabled"):
        return None
    start = state.get("timer_start_ts")
    duration = state.get("timer_duration")
    if not start or not duration:
        return None
    remaining = int(duration - (time.time() - start))
    return max(0, remaining)


def tick_timer_locked(state: Dict[str, Any]) -> Optional[int]:
    remaining = get_timer_remaining(state)
    if remaining is None:
        return None
    if remaining > 0:
        return remaining
    if state.get("timer_expired"):
        return 0
    state["timer_expired"] = True
    if state.get("late_submit_policy") == "lock_after_timer":
        state["submissions_locked"] = True
    if not state.get("auto_advance"):
        return 0
    if state.get("phase") != "in_round":
        return 0

    mode = state.get("mode")
    if mode == "votebattle":
        if state.get("votebattle_phase") == "submit":
            if state.get("votebattle_entries"):
                state["votebattle_phase"] = "vote"
                state["submissions_locked"] = False
                reset_timer_locked(state, state.get("vote_timer_seconds"))
                state["host_message"] = "Timer: Vote Battle voting started."
            return 0
        if state.get("votebattle_phase") == "vote":
            compute_results_locked()
            state["phase"] = "revealed"
            state["host_message"] = "Timer: Results revealed."
            return 0

    if mode == "spyfall":
        if state.get("spyfall_phase") == "question":
            if not state.get("spyfall_auto_start_vote_on_timer", True):
                return 0
            if state.get("players"):
                state["spyfall_phase"] = "vote"
                state["submissions"] = {}
                state["submissions_locked"] = False
                reset_timer_locked(state, state.get("vote_timer_seconds"))
                state["host_message"] = "Timer: Spyfall voting started."
            return 0
        if state.get("spyfall_phase") == "vote":
            compute_results_locked()
            state["phase"] = "revealed"
            state["host_message"] = "Timer: Results revealed."
            return 0

    if mode in ("trivia_buzzer", "team_trivia"):
        trivia_phase = state.get("trivia_buzzer_phase")
        if trivia_phase == "buzz":
            if state.get("buzz_winner_pid"):
                state["trivia_buzzer_phase"] = "answer"
                state["submissions_locked"] = False
                reset_timer_locked(state, state.get("vote_timer_seconds"))
                state["host_message"] = "Timer: Answer phase started."
            else:
                compute_results_locked()
                state["phase"] = "revealed"
                state["host_message"] = "Timer: No buzz."
            return 0
        if trivia_phase in ("answer", "steal"):
            compute_results_locked()
            state["phase"] = "revealed"
            state["host_message"] = "Timer: Results revealed."
            return 0

    if mode == "mafia":
        return 0

    compute_results_locked()
    state["phase"] = "revealed"
    state["host_message"] = "Timer: Results revealed."
    return 0


def get_scoreboard(players: Dict[str, Dict[str, str]], scores: Dict[str, int]) -> List[Dict[str, Any]]:
    rows = []
    for pid, info in players.items():
        rows.append({"pid": pid, "name": info.get("name", "Unknown"), "score": scores.get(pid, 0)})
    rows.sort(key=lambda row: (-row["score"], row["name"].lower()))
    return rows


def ensure_team_names(state: Dict[str, Any]) -> None:
    count = int(state.get("team_count", 2))
    names = state.get("team_names") or {}
    for team_id in range(1, count + 1):
        names.setdefault(team_id, f"Team {team_id}")
    state["team_names"] = names


def assign_team_for_new_player(state: Dict[str, Any], pid: str) -> None:
    if not state.get("teams_enabled"):
        return
    count = int(state.get("team_count", 2))
    ensure_team_names(state)
    counts = {team_id: 0 for team_id in range(1, count + 1)}
    for _, team_id in state.get("teams", {}).items():
        if team_id in counts:
            counts[team_id] += 1
    min_count = min(counts.values()) if counts else 0
    candidates = [team_id for team_id, value in counts.items() if value == min_count]
    team_id = random.choice(candidates) if candidates else 1
    state.setdefault("teams", {})[pid] = team_id


def randomize_teams(state: Dict[str, Any]) -> None:
    if not state.get("teams_enabled"):
        return
    count = int(state.get("team_count", 2))
    ensure_team_names(state)
    pids = list(state.get("players", {}).keys())
    random.shuffle(pids)
    state["teams"] = {}
    for idx, pid in enumerate(pids):
        state["teams"][pid] = (idx % count) + 1


def get_team_scoreboard(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not state.get("teams_enabled"):
        return []
    count = int(state.get("team_count", 2))
    ensure_team_names(state)
    totals = {team_id: 0 for team_id in range(1, count + 1)}
    for pid, score in state.get("scores", {}).items():
        team_id = state.get("teams", {}).get(pid)
        if team_id in totals:
            totals[team_id] += score
    rows = []
    for team_id, score in totals.items():
        rows.append(
            {
                "team_id": team_id,
                "name": state.get("team_names", {}).get(team_id, f"Team {team_id}"),
                "score": score,
            }
        )
    rows.sort(key=lambda row: (-row["score"], row["name"].lower()))
    return rows


def get_team_label(state: Dict[str, Any], pid: str) -> Optional[str]:
    if not state.get("teams_enabled"):
        return None
    team_id = state.get("teams", {}).get(pid)
    if not team_id:
        return None
    return state.get("team_names", {}).get(team_id, f"Team {team_id}")


def get_team_name(state: Dict[str, Any], team_id: Optional[int]) -> str:
    if not team_id:
        return "Team"
    return state.get("team_names", {}).get(team_id, f"Team {team_id}")


def get_active_team_ids(state: Dict[str, Any]) -> List[int]:
    if not state.get("teams_enabled"):
        return []
    players = state.get("players", {})
    team_map = state.get("teams", {})
    seen: List[int] = []
    for pid in players:
        team_id = team_map.get(pid)
        if team_id and team_id not in seen:
            seen.append(team_id)
    seen.sort()
    return seen


def get_team_members(state: Dict[str, Any], team_id: int) -> List[str]:
    players = state.get("players", {})
    members = [pid for pid, tid in state.get("teams", {}).items() if tid == team_id and pid in players]
    members.sort(key=lambda pid: players.get(pid, {}).get("name", "").lower())
    return members


def apply_score_delta(state: Dict[str, Any], pid: str, delta: int, *, floor_zero: bool = False) -> None:
    if pid not in state.get("players", {}):
        return
    current = state.get("scores", {}).get(pid, 0)
    updated = current + delta
    if floor_zero:
        updated = max(0, updated)
    state.setdefault("scores", {})[pid] = updated


def apply_team_score_delta(
    state: Dict[str, Any], team_id: Optional[int], delta: int, *, floor_zero: bool = False
) -> List[str]:
    if team_id is None:
        return []
    members = get_team_members(state, team_id)
    for pid in members:
        apply_score_delta(state, pid, delta, floor_zero=floor_zero)
    return members


def normalize_jeopardy_answer(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    cleaned = " ".join(cleaned.split())
    for prefix in ("a ", "an ", "the "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def jeopardy_answer_matches(guess: str, answer: str) -> bool:
    return normalize_jeopardy_answer(guess) == normalize_jeopardy_answer(answer)


def build_jeopardy_board() -> List[Dict[str, Any]]:
    categories = JEOPARDY_CATEGORIES[:]
    random.shuffle(categories)
    board = []
    for category in categories:
        clues = []
        for idx, clue in enumerate(category.get("clues", [])):
            clues.append(
                {
                    "value": (idx + 1) * 100,
                    "question": str(clue.get("question", "")),
                    "answer": str(clue.get("answer", "")),
                    "used": False,
                }
            )
        board.append({"category": str(category.get("category", "Category")), "clues": clues})
    return board


def get_jeopardy_clue(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    selected = state.get("jeopardy_selected")
    board = state.get("jeopardy_board", [])
    if not selected:
        return None
    cat_idx = selected.get("cat_idx")
    clue_idx = selected.get("clue_idx")
    if not isinstance(cat_idx, int) or not isinstance(clue_idx, int):
        return None
    if cat_idx < 0 or clue_idx < 0:
        return None
    if cat_idx >= len(board):
        return None
    clues = board[cat_idx].get("clues", [])
    if clue_idx >= len(clues):
        return None
    return clues[clue_idx]


def mark_jeopardy_selected_used(state: Dict[str, Any]) -> None:
    clue = get_jeopardy_clue(state)
    if clue is not None:
        clue["used"] = True


def next_captain_for_team(state: Dict[str, Any], team_id: int, current_pid: Optional[str]) -> Optional[str]:
    members = get_team_members(state, team_id)
    if not members:
        return None
    if current_pid in members:
        idx = members.index(current_pid)
        return members[(idx + 1) % len(members)]
    return members[0]


def rotate_relay_captains(state: Dict[str, Any]) -> Dict[int, str]:
    captains = {}
    previous = state.get("relay_captains", {})
    for team_id in get_active_team_ids(state):
        captains[team_id] = next_captain_for_team(state, team_id, previous.get(team_id))
    state["relay_captains"] = captains
    return captains


def build_trivia_pool(
    count: int, *, manual_question: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    pool: List[Dict[str, Any]] = []
    used_questions = set()
    if manual_question:
        pool.append(manual_question)
        used_questions.add(manual_question.get("question", "").strip().lower())
    questions = TRIVIA_QUESTIONS[:]
    random.shuffle(questions)
    for question in questions:
        if len(pool) >= count:
            break
        text = str(question.get("question", "")).strip().lower()
        if not text or text in used_questions:
            continue
        pool.append(
            {
                "question": str(question.get("question", "")),
                "options": list(question.get("options", [])),
                "correct_index": int(question.get("answer_index", 0)),
            }
        )
        used_questions.add(text)
    if not pool:
        pool.append(
            {
                "question": "What color is the sky on a clear day?",
                "options": ["Green", "Blue", "Red", "Yellow"],
                "correct_index": 1,
            }
        )
    return pool[: max(1, count)]


def get_draft_question(state: Dict[str, Any], team_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if team_id is None:
        return None
    picks = state.get("draft_picks", {})
    pool = state.get("draft_pool", [])
    idx = picks.get(team_id)
    if isinstance(idx, int) and 0 <= idx < len(pool):
        return pool[idx]
    return None


def record_draft_pick(state: Dict[str, Any], team_id: int, question_idx: int) -> bool:
    pool = state.get("draft_pool", [])
    if question_idx < 0 or question_idx >= len(pool):
        return False
    picks = state.setdefault("draft_picks", {})
    if team_id in picks:
        return False
    if question_idx in picks.values():
        return False
    picks[team_id] = question_idx
    advance_draft_pick_team(state)
    return True


def advance_draft_pick_team(state: Dict[str, Any]) -> None:
    order = state.get("draft_turn_order", [])
    picks = state.get("draft_picks", {})
    pool = state.get("draft_pool", [])
    if len(picks) >= len(pool):
        state["draft_pick_team_id"] = None
        return
    idx = int(state.get("draft_turn_idx", 0))
    idx += 1
    while idx < len(order) and order[idx] in picks:
        idx += 1
    state["draft_turn_idx"] = idx
    state["draft_pick_team_id"] = order[idx] if idx < len(order) else None


def pick_first_correct_team(
    choices: Dict[int, int], correct_index: Optional[int]
) -> Optional[int]:
    if correct_index is None:
        return None
    for team_id, choice in choices.items():
        if choice == correct_index:
            return team_id
    return None


def resolve_estimation_winners(
    submissions: Dict[Any, Any], target: Optional[int], price_is_right: bool
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    guesses: List[Dict[str, Any]] = []
    if target is None:
        return [], guesses
    for key, value in submissions.items():
        try:
            guess = int(value)
        except (TypeError, ValueError):
            continue
        guesses.append({"key": key, "guess": guess, "distance": abs(guess - target), "over": guess > target})
    if not guesses:
        return [], guesses
    eligible = guesses
    if price_is_right:
        eligible = [row for row in guesses if not row["over"]]
        if not eligible:
            eligible = guesses
    closest = min(row["distance"] for row in eligible)
    winners = [row["key"] for row in eligible if row["distance"] == closest]
    return winners, guesses

def select_buzz_winner(
    existing_pid: Optional[str],
    existing_ts: Optional[float],
    candidate_pid: str,
    candidate_ts: float,
) -> Tuple[str, float]:
    if existing_pid is None or existing_ts is None:
        return candidate_pid, candidate_ts
    if candidate_ts < existing_ts:
        return candidate_pid, candidate_ts
    return existing_pid, existing_ts


def pick_first_correct_steal(steal_attempts: Dict[str, int], correct_index: Optional[int]) -> Optional[str]:
    if correct_index is None:
        return None
    for pid, choice in steal_attempts.items():
        if choice == correct_index:
            return pid
    return None


def compute_trivia_buzzer_outcome(
    correct_index: Optional[int],
    buzz_winner_pid: Optional[str],
    answer_pid: Optional[str],
    answer_choice: Optional[int],
    steal_attempts: Dict[str, int],
) -> Dict[str, Any]:
    outcome = {
        "buzz_correct": False,
        "steal_pid": None,
        "scoring_pid": None,
        "points": 0,
    }
    if correct_index is None or not buzz_winner_pid:
        return outcome
    if answer_choice is None:
        steal_pid = pick_first_correct_steal(steal_attempts, correct_index)
        if steal_pid:
            outcome.update({"steal_pid": steal_pid, "scoring_pid": steal_pid, "points": 1})
        return outcome
    if answer_choice == correct_index:
        scorer = answer_pid or buzz_winner_pid
        outcome.update({"buzz_correct": True, "scoring_pid": scorer, "points": 2})
        return outcome
    steal_pid = pick_first_correct_steal(steal_attempts, correct_index)
    if steal_pid:
        outcome.update({"steal_pid": steal_pid, "scoring_pid": steal_pid, "points": 1})
    return outcome


def pool_key_for_mode(mode: str) -> str:
    if mode in ("trivia", "trivia_buzzer", "team_trivia"):
        return "trivia"
    if mode == "spyfall":
        return "spyfall"
    return mode


def draw_from_pool(state: Dict[str, Any], key: str, n: int) -> int:
    if n <= 0:
        return 0
    prompt_bags = state.setdefault("prompt_bags", {})
    prompt_last = state.setdefault("prompt_last", {})
    bag = prompt_bags.get(key)
    if not bag:
        bag = list(range(n))
        random.shuffle(bag)
        last = prompt_last.get(key)
        if n >= 2 and last is not None and bag and bag[0] == last:
            bag[0], bag[1] = bag[1], bag[0]
        prompt_bags[key] = bag
    choice = bag.pop(0)
    prompt_last[key] = choice
    return choice


def reset_pool(state: Dict[str, Any], key: str) -> None:
    state.setdefault("prompt_bags", {}).pop(key, None)
    state.setdefault("prompt_last", {}).pop(key, None)


def pick_prompt_for_mode(mode: str, state: Dict[str, Any]) -> Tuple[str, List[str], Optional[int]]:
    if mode == "mlt":
        if MLT_PROMPTS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(MLT_PROMPTS))
            prompt = MLT_PROMPTS[idx]
        else:
            prompt = "Who is most likely to plan the next party?"
        return prompt, [], None
    if mode == "wyr":
        if WYR_PROMPTS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(WYR_PROMPTS))
            choice = WYR_PROMPTS[idx]
            return "Would you rather...", [choice["a"], choice["b"]], None
        return "Would you rather...", ["Option A", "Option B"], None
    if mode in ("trivia", "trivia_buzzer", "team_trivia"):
        if TRIVIA_QUESTIONS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(TRIVIA_QUESTIONS))
            question = TRIVIA_QUESTIONS[idx]
        else:
            question = {
                "question": "What color is the sky on a clear day?",
                "options": ["Green", "Blue", "Red", "Yellow"],
                "answer_index": 1,
            }
        return question["question"], list(question["options"]), int(question["answer_index"])
    if mode == "hotseat":
        if HOTSEAT_PROMPTS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(HOTSEAT_PROMPTS))
            prompt = HOTSEAT_PROMPTS[idx]
        else:
            prompt = "Hot seat: Share your hottest take."
        return prompt, [], None
    if mode == "quickdraw":
        if QUICKDRAW_PROMPTS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(QUICKDRAW_PROMPTS))
            prompt = QUICKDRAW_PROMPTS[idx]
        else:
            prompt = "Name a party snack."
        return prompt, [], None
    if mode == "wavelength":
        if SPECTRUM_PROMPTS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(SPECTRUM_PROMPTS))
            prompt = SPECTRUM_PROMPTS[idx]
        else:
            prompt = "Cold <-> Hot"
        return prompt, [], None
    if mode == "votebattle":
        if VOTEBATTLE_PROMPTS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(VOTEBATTLE_PROMPTS))
            prompt = VOTEBATTLE_PROMPTS[idx]
        else:
            prompt = "Best excuse for being late."
        return prompt, [], None
    if mode == "spyfall":
        if SPYFALL_LOCATIONS:
            idx = draw_from_pool(state, pool_key_for_mode(mode), len(SPYFALL_LOCATIONS))
            choice = SPYFALL_LOCATIONS[idx]
        else:
            choice = {"location": "Movie Theater"}
        roles = choice.get("roles") or []
        return str(choice.get("location", "Movie Theater")), [str(role) for role in roles], None
    if mode == "mafia":
        return "Mafia: Night falls...", [], None
    return "Waiting for host", [], None


def resolve_prompt_for_mode(
    mode: str, state: Dict[str, Any]
) -> Tuple[Optional[str], List[str], Optional[int], Optional[int], Optional[str]]:
    if mode == "mafia":
        prompt, options, correct_index = pick_prompt_for_mode(mode, state)
        return prompt, options, correct_index, None, None
    prompt_mode = state.get("prompt_mode", "random")
    if prompt_mode != "manual":
        prompt, options, correct_index = pick_prompt_for_mode(mode, state)
        return prompt, options, correct_index, None, None

    prompt_text = str(state.get("manual_prompt_text", "")).strip()
    if not prompt_text:
        return None, [], None, None, "Manual prompt text is required."

    if mode == "wyr":
        option_a = str(state.get("manual_wyr_a", "")).strip()
        option_b = str(state.get("manual_wyr_b", "")).strip()
        if not option_a or not option_b:
            return None, [], None, None, "Manual WYR options A and B are required."
        return prompt_text, [option_a, option_b], None, None, None

    if mode in ("trivia", "trivia_buzzer", "team_trivia"):
        options = [
            str(state.get("manual_trivia_0", "")).strip(),
            str(state.get("manual_trivia_1", "")).strip(),
            str(state.get("manual_trivia_2", "")).strip(),
            str(state.get("manual_trivia_3", "")).strip(),
        ]
        if any(not opt for opt in options):
            return None, [], None, None, "Manual trivia requires 4 options."
        correct_raw = state.get("manual_correct_index", "")
        try:
            correct_index = int(correct_raw)
        except (TypeError, ValueError):
            return None, [], None, None, "Manual trivia requires a correct index (0-3)."
        if correct_index < 0 or correct_index > 3:
            return None, [], None, None, "Manual trivia correct index must be 0-3."
        return prompt_text, options, correct_index, None, None

    if mode == "wavelength":
        manual_target = None
        if state.get("manual_wavelength_target_enabled"):
            raw_target = state.get("manual_wavelength_target", "")
            try:
                manual_target = int(raw_target)
            except (TypeError, ValueError):
                return None, [], None, None, "Manual target must be a number from 0 to 100."
            if manual_target < 0 or manual_target > 100:
                return None, [], None, None, "Manual target must be 0 to 100."
        return prompt_text, [], None, manual_target, None

    if mode == "spyfall":
        roles = spyfall_roles_for_location(prompt_text)
        return prompt_text, roles, None, None, None

    return prompt_text, [], None, None, None


def spyfall_roles_for_location(location: str) -> List[str]:
    for entry in SPYFALL_LOCATIONS:
        if str(entry.get("location", "")).strip().lower() == location.strip().lower():
            roles = entry.get("roles") or []
            return [str(role) for role in roles if str(role).strip()]
    return ["Local", "Worker", "Visitor", "Manager", "Regular", "Rookie"]


def assign_spyfall_roles(state: Dict[str, Any], roles_pool: List[str]) -> None:
    pids = list(state.get("players", {}).keys())
    if not pids:
        return
    spy_pid = random.choice(pids)
    state["spyfall_spy_pid"] = spy_pid
    state["spyfall_roles"] = {}
    pool = roles_pool[:] if roles_pool else spyfall_roles_for_location(state.get("prompt", ""))
    if not pool:
        pool = ["Local", "Visitor"]
    random.shuffle(pool)
    idx = 0
    for pid in pids:
        if pid == spy_pid:
            continue
        role = pool[idx % len(pool)]
        state["spyfall_roles"][pid] = role
        idx += 1


def assign_mafia_roles(
    pids: List[str],
    *,
    seer_enabled: bool,
    auto_wolf_count: bool,
    wolf_count: int,
) -> Dict[str, str]:
    if len(pids) < MAFIA_MIN_PLAYERS:
        return {}
    roles = {}
    shuffled = pids[:]
    random.shuffle(shuffled)
    if auto_wolf_count:
        wolf_count = 2 if len(pids) >= 7 else 1
    else:
        wolf_count = int(wolf_count or 1)
        wolf_count = max(1, min(2, wolf_count))
    seer_count = 1 if seer_enabled and len(pids) >= 4 else 0
    max_wolves = max(1, len(pids) - seer_count - 1)
    wolf_count = min(wolf_count, max_wolves)
    for pid in shuffled[:wolf_count]:
        roles[pid] = "werewolf"
    offset = wolf_count
    for pid in shuffled[offset : offset + seer_count]:
        roles[pid] = "seer"
    for pid in shuffled[offset + seer_count :]:
        roles[pid] = "villager"
    return roles


def resolve_mafia_vote(votes: Dict[str, Any], alive: List[str]) -> Optional[str]:
    valid = list(alive)
    tally = build_tally(votes, valid)
    winners, _ = pick_winners_from_tally(tally)
    if not winners:
        return None
    return random.choice(winners)


def check_mafia_win(state: Dict[str, Any]) -> Optional[str]:
    alive = state.get("mafia_alive", [])
    if not alive:
        return None
    roles = state.get("mafia_roles", {})
    wolves = [pid for pid in alive if roles.get(pid) == "werewolf"]
    villagers = [pid for pid in alive if roles.get(pid) != "werewolf"]
    if not wolves:
        return "villagers"
    if len(wolves) >= len(villagers):
        return "werewolves"
    return None


def start_new_round_locked(mode: str) -> bool:
    prompt, options, correct_index, manual_target, err = resolve_prompt_for_mode(mode, STATE)
    if err:
        STATE["host_message"] = err
        return False
    if prompt is None:
        STATE["host_message"] = "Prompt could not be loaded."
        return False
    if mode == "team_trivia" and not STATE.get("teams_enabled"):
        STATE["host_message"] = "Team Trivia requires teams enabled."
        return False
    if mode == "mafia" and len(STATE.get("players", {})) < MAFIA_MIN_PLAYERS:
        STATE["host_message"] = f"Mafia needs at least {MAFIA_MIN_PLAYERS} players."
        return False
    STATE["round_id"] += 1
    STATE["mode"] = mode
    STATE["phase"] = "in_round"
    STATE["prompt"] = prompt
    STATE["options"] = options
    STATE["correct_index"] = correct_index
    STATE["trivia_buzzer_phase"] = None
    STATE["trivia_buzzer_question"] = ""
    STATE["trivia_buzzer_options"] = []
    STATE["trivia_buzzer_correct_index"] = None
    STATE["buzz_winner_pid"] = None
    STATE["buzz_winner_team_id"] = None
    STATE["buzz_ts"] = None
    STATE["answer_pid"] = None
    STATE["answer_team_id"] = None
    STATE["answer_choice"] = None
    STATE["steal_attempts"] = {}
    STATE["trivia_buzzer_result"] = None
    STATE["submissions_locked"] = False
    STATE["round_start_ts"] = time.time()
    if mode == "wavelength":
        STATE["wavelength_target"] = manual_target if manual_target is not None else random.randint(0, 100)
    else:
        STATE["wavelength_target"] = None
    STATE["submissions"] = {}
    STATE["votebattle_phase"] = None
    STATE["votebattle_entries"] = {}
    STATE["votebattle_votes"] = {}
    STATE["votebattle_order"] = []
    STATE["votebattle_counter"] = 0
    STATE["spyfall_phase"] = None
    STATE["spyfall_location"] = ""
    STATE["spyfall_spy_pid"] = None
    STATE["spyfall_roles"] = {}
    STATE["mafia_phase"] = None
    STATE["mafia_roles"] = {}
    STATE["mafia_alive"] = []
    STATE["mafia_wolf_votes"] = {}
    STATE["mafia_day_votes"] = {}
    STATE["mafia_seer_results"] = {}
    STATE["mafia_last_eliminated"] = None
    STATE["last_result"] = None
    reset_timer_locked(STATE, STATE.get("timer_seconds"))

    if mode in ("trivia_buzzer", "team_trivia"):
        STATE["trivia_buzzer_phase"] = "buzz"
        STATE["trivia_buzzer_question"] = prompt
        STATE["trivia_buzzer_options"] = options
        STATE["trivia_buzzer_correct_index"] = correct_index

    if mode == "votebattle":
        STATE["votebattle_phase"] = "submit"
    if mode == "spyfall":
        STATE["spyfall_phase"] = "question"
        STATE["spyfall_location"] = prompt
        assign_spyfall_roles(STATE, options)
    if mode == "mafia":
        roles = assign_mafia_roles(
            list(STATE.get("players", {}).keys()),
            seer_enabled=STATE.get("mafia_seer_enabled", True),
            auto_wolf_count=STATE.get("mafia_auto_wolf_count", True),
            wolf_count=STATE.get("mafia_wolf_count", 1),
        )
        STATE["mafia_roles"] = roles
        STATE["mafia_alive"] = list(STATE.get("players", {}).keys())
        STATE["mafia_phase"] = "night"
    return True


def set_manual_prompt_from_random_locked(mode: str) -> None:
    preview_state = {
        "prompt_bags": copy.deepcopy(STATE.get("prompt_bags", {})),
        "prompt_last": copy.deepcopy(STATE.get("prompt_last", {})),
    }
    prompt, options, correct_index = pick_prompt_for_mode(mode, preview_state)
    STATE["prompt_mode"] = "manual"
    STATE["manual_prompt_text"] = prompt
    if mode == "wyr":
        STATE["manual_wyr_a"] = options[0] if len(options) > 0 else ""
        STATE["manual_wyr_b"] = options[1] if len(options) > 1 else ""
    elif mode in ("trivia", "trivia_buzzer", "team_trivia"):
        STATE["manual_trivia_0"] = options[0] if len(options) > 0 else ""
        STATE["manual_trivia_1"] = options[1] if len(options) > 1 else ""
        STATE["manual_trivia_2"] = options[2] if len(options) > 2 else ""
        STATE["manual_trivia_3"] = options[3] if len(options) > 3 else ""
        STATE["manual_correct_index"] = correct_index


def build_history_entry(state: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    players = state.get("players", {})
    def name_for(pid: str) -> str:
        return players.get(pid, {}).get("name", "Unknown")

    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "round_id": result.get("round_id"),
        "mode": result.get("mode"),
        "prompt": result.get("prompt"),
    }

    mode = result.get("mode")
    if mode == "mlt":
        entry["winners"] = [name_for(pid) for pid in result.get("winners", [])]
        entry["max_votes"] = result.get("max_votes", 0)
    elif mode == "wyr":
        entry["tally"] = result.get("tally", {})
        entry["majority"] = result.get("majority")
    elif mode == "trivia":
        entry["winners"] = [name_for(pid) for pid in result.get("winners", [])]
        entry["correct_index"] = result.get("correct_index")
    elif mode in ("trivia_buzzer", "team_trivia"):
        buzz_pid = result.get("buzz_winner_pid")
        answer_pid = result.get("answer_pid")
        entry["buzz_winner"] = name_for(buzz_pid) if buzz_pid else None
        entry["answer"] = name_for(answer_pid) if answer_pid else None
        entry["correct_index"] = result.get("correct_index")
        entry["points"] = result.get("points", 0)
        entry["scoring"] = [name_for(pid) for pid in result.get("scoring_pids", [])]
    elif mode == "hotseat":
        entry["answers"] = result.get("answers", [])
    elif mode == "quickdraw":
        entry["unique_winners"] = [name_for(pid) for pid in result.get("unique_pids", [])]
        entry["groups"] = result.get("groups", [])
    elif mode == "wavelength":
        entry["winners"] = [name_for(pid) for pid in result.get("winners", [])]
        entry["target"] = result.get("target")
    elif mode == "votebattle":
        entry["winners"] = [name_for(pid) for pid in result.get("winners", [])]
        entry["entries"] = result.get("entries", [])
    elif mode == "spyfall":
        spy_pid = result.get("spy_pid")
        entry["spy"] = name_for(spy_pid) if spy_pid else "Unknown"
        entry["spy_caught"] = result.get("spy_caught", False)
        entry["winners"] = [name_for(pid) for pid in result.get("winners", [])]
        entry["tally"] = result.get("tally", {})
    elif mode == "mafia":
        entry["winner"] = result.get("winner")
        entry["roles"] = result.get("roles", {})
        entry["alive"] = [name_for(pid) for pid in result.get("alive", [])]
    return entry


def append_history_locked(state: Dict[str, Any], result: Dict[str, Any]) -> None:
    entry = build_history_entry(state, result)
    history = state.get("history", [])
    if history and history[-1].get("round_id") == entry.get("round_id") and history[-1].get("mode") == entry.get("mode"):
        history[-1] = entry
    else:
        history.append(entry)
    state["history"] = history


def build_recap_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    players = []
    for pid, info in state.get("players", {}).items():
        players.append(
            {
                "pid": pid,
                "name": info.get("name", "Unknown"),
                "score": state.get("scores", {}).get(pid, 0),
                "team": get_team_label(state, pid),
            }
        )
    players.sort(key=lambda row: (-row["score"], row["name"].lower()))
    return {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "players": players,
        "teams": get_team_scoreboard(state),
        "history": state.get("history", []),
    }


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

    elif mode in ("trivia_buzzer", "team_trivia"):
        correct_index = STATE.get("trivia_buzzer_correct_index")
        outcome = compute_trivia_buzzer_outcome(
            correct_index,
            STATE.get("buzz_winner_pid"),
            STATE.get("answer_pid"),
            STATE.get("answer_choice"),
            STATE.get("steal_attempts", {}),
        )
        scoring_pid = outcome.get("scoring_pid")
        points = int(outcome.get("points", 0))
        scoring_pids: List[str] = []
        scoring_team_id = None
        if scoring_pid:
            if mode == "team_trivia":
                teams = STATE.get("teams", {})
                scoring_team_id = teams.get(scoring_pid)
                if scoring_team_id is not None:
                    scoring_pids = [pid for pid, team_id in teams.items() if team_id == scoring_team_id]
            else:
                scoring_pids = [scoring_pid]
        for pid in scoring_pids:
            STATE["scores"][pid] = STATE["scores"].get(pid, 0) + points
        result.update(
            {
                "correct_index": correct_index,
                "buzz_winner_pid": STATE.get("buzz_winner_pid"),
                "buzz_winner_team_id": STATE.get("buzz_winner_team_id"),
                "buzz_ts": STATE.get("buzz_ts"),
                "answer_pid": STATE.get("answer_pid"),
                "answer_team_id": STATE.get("answer_team_id"),
                "answer_choice": STATE.get("answer_choice"),
                "steal_attempts": dict(STATE.get("steal_attempts", {})),
                "buzz_correct": outcome.get("buzz_correct", False),
                "steal_pid": outcome.get("steal_pid"),
                "scoring_pids": scoring_pids,
                "scoring_team_id": scoring_team_id,
                "points": points,
            }
        )
        STATE["trivia_buzzer_result"] = result

    elif mode == "hotseat":
        answers = []
        for pid, answer in submissions.items():
            name = players.get(pid, {}).get("name", "Unknown")
            answers.append({"pid": pid, "name": name, "answer": str(answer)})
        result.update({"answers": answers})

    elif mode == "quickdraw":
        answers = []
        normalized_map: Dict[str, List[str]] = {}
        for pid, answer in submissions.items():
            raw = str(answer).strip()
            normalized = normalize_text(raw)
            normalized_map.setdefault(normalized, []).append(pid)
            answers.append(
                {"pid": pid, "name": players.get(pid, {}).get("name", "Unknown"), "answer": raw, "normalized": normalized}
            )

        unique_pids = set(unique_answer_pids(submissions))

        if STATE.get("quickdraw_scoring") == "unique":
            for pid in unique_pids:
                STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1

        groups = []
        for normalized, pids in normalized_map.items():
            if not normalized:
                continue
            names = [players.get(pid, {}).get("name", "Unknown") for pid in pids]
            names.sort(key=lambda name: name.lower())
            display = next((row["answer"] for row in answers if row["normalized"] == normalized), normalized)
            groups.append(
                {
                    "answer": display,
                    "pids": list(pids),
                    "names": names,
                    "count": len(pids),
                    "unique": len(pids) == 1,
                }
            )
        groups.sort(key=lambda row: (-row["count"], row["answer"].lower()))

        result.update(
            {
                "answers": answers,
                "groups": groups,
                "unique_pids": list(unique_pids),
                "scoring": STATE.get("quickdraw_scoring", "unique"),
            }
        )

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

    elif mode == "votebattle":
        entries = []
        votes = STATE.get("votebattle_votes", {})
        order = STATE.get("votebattle_order", [])
        counts: Dict[int, int] = {entry.get("id"): 0 for entry in order}
        for _, entry_id in votes.items():
            if entry_id in counts:
                counts[entry_id] += 1
        winners: List[str] = []
        if counts:
            max_votes = max(counts.values())
            for entry in order:
                entry_id = entry.get("id")
                if counts.get(entry_id, 0) == max_votes and max_votes > 0:
                    pid = entry.get("pid")
                    if pid in players:
                        winners.append(pid)
        for pid in set(winners):
            STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
        for entry in order:
            entry_id = entry.get("id")
            entries.append(
                {
                    "id": entry_id,
                    "pid": entry.get("pid"),
                    "text": entry.get("text", ""),
                    "votes": counts.get(entry_id, 0),
                }
            )
        result.update({"entries": entries, "winners": winners})

    elif mode == "spyfall":
        spy_pid = STATE.get("spyfall_spy_pid")
        tally = build_tally(submissions, list(players.keys()))
        winners, max_votes = pick_winners_from_tally(tally)
        spy_caught = bool(spy_pid in winners and max_votes > 0)
        if spy_pid:
            if spy_caught:
                for pid in players:
                    if pid != spy_pid:
                        STATE["scores"][pid] = STATE["scores"].get(pid, 0) + 1
            else:
                STATE["scores"][spy_pid] = STATE["scores"].get(spy_pid, 0) + 2
        result.update(
            {
                "tally": tally,
                "winners": winners,
                "max_votes": max_votes,
                "spy_pid": spy_pid,
                "spy_caught": spy_caught,
                "location": STATE.get("spyfall_location", ""),
            }
        )

    elif mode == "mafia":
        winner = check_mafia_win(STATE)
        result.update(
            {
                "winner": winner,
                "roles": STATE.get("mafia_roles", {}),
                "alive": list(STATE.get("mafia_alive", [])),
                "last_eliminated": STATE.get("mafia_last_eliminated"),
            }
        )

    STATE["last_result"] = result
    append_history_locked(STATE, result)
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
    if mode in ("trivia_buzzer", "team_trivia"):
        options = result.get("options", [])
        correct = result.get("correct_index")
        buzz_pid = result.get("buzz_winner_pid")
        buzz_name = players.get(buzz_pid, {}).get("name", "Unknown") if buzz_pid else None
        buzz_team_id = result.get("buzz_winner_team_id")
        buzz_team_label = state.get("team_names", {}).get(buzz_team_id, f"Team {buzz_team_id}") if buzz_team_id else None
        answer_pid = result.get("answer_pid")
        answer_name = players.get(answer_pid, {}).get("name", "Unknown") if answer_pid else None
        answer_team_id = result.get("answer_team_id")
        answer_team_label = (
            state.get("team_names", {}).get(answer_team_id, f"Team {answer_team_id}") if answer_team_id else None
        )
        answer_choice = result.get("answer_choice")
        correct_text = options[correct] if isinstance(correct, int) and 0 <= correct < len(options) else "Unknown"
        option_labels = ["A", "B", "C", "D"]
        answer_label = None
        if isinstance(answer_choice, int) and 0 <= answer_choice < len(options):
            answer_label = f"{option_labels[answer_choice]}: {options[answer_choice]}"
        steal_pid = result.get("steal_pid")
        steal_name = players.get(steal_pid, {}).get("name", "Unknown") if steal_pid else None
        steal_team_id = state.get("teams", {}).get(steal_pid) if steal_pid else None
        steal_team_label = (
            state.get("team_names", {}).get(steal_team_id, f"Team {steal_team_id}") if steal_team_id else None
        )
        scoring_pids = result.get("scoring_pids", [])
        scoring_names = [players.get(pid, {}).get("name", "Unknown") for pid in scoring_pids]
        scoring_team_id = result.get("scoring_team_id")
        scoring_team_label = (
            state.get("team_names", {}).get(scoring_team_id, f"Team {scoring_team_id}") if scoring_team_id else None
        )
        return {
            "mode": mode,
            "correct_text": correct_text,
            "buzz_name": buzz_name,
            "buzz_team": buzz_team_label,
            "answer_name": answer_name,
            "answer_team": answer_team_label,
            "answer_label": answer_label,
            "buzz_correct": result.get("buzz_correct", False),
            "steal_name": steal_name,
            "steal_team": steal_team_label,
            "scoring_names": scoring_names,
            "scoring_team": scoring_team_label,
            "points": result.get("points", 0),
        }
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
    if mode == "quickdraw":
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
        groups = []
        for row in result.get("groups", []):
            groups.append(
                {
                    "answer": row.get("answer", ""),
                    "count": row.get("count", 0),
                    "players": row.get("names", []),
                    "unique": row.get("unique", False),
                }
            )
        groups.sort(key=lambda row: (-row["count"], row["answer"].lower()))
        return {"mode": "quickdraw", "answer_groups": groups, "entries": answers}
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
    if mode == "votebattle":
        entries = []
        winners = set(result.get("winners", []))
        for row in result.get("entries", []):
            pid = row.get("pid")
            entry = {
                "text": row.get("text", ""),
                "votes": row.get("votes", 0),
                "winner": pid in winners,
            }
            if reveal_authors:
                entry["author"] = players.get(pid, {}).get("name", "Unknown")
            entries.append(entry)
        entries.sort(key=lambda row: (-row["votes"], row["text"].lower()))
        winner_names = [players.get(pid, {}).get("name", "Unknown") for pid in winners]
        return {"mode": "votebattle", "entries": entries, "winners": winner_names}
    if mode == "spyfall":
        tally = result.get("tally", {})
        rows = []
        for pid, votes in tally.items():
            rows.append({"name": players.get(pid, {}).get("name", "Unknown"), "votes": votes})
        rows.sort(key=lambda row: (-row["votes"], row["name"].lower()))
        spy_pid = result.get("spy_pid")
        spy_name = players.get(spy_pid, {}).get("name", "Unknown") if spy_pid else "Unknown"
        return {
            "mode": "spyfall",
            "tally_rows": rows,
            "spy_name": spy_name,
            "location": result.get("location") or "",
            "spy_caught": result.get("spy_caught", False),
        }
    if mode == "mafia":
        roles = []
        if state.get("mafia_reveal_roles_on_end", True):
            for pid, role in result.get("roles", {}).items():
                roles.append({"name": players.get(pid, {}).get("name", "Unknown"), "role": role})
            roles.sort(key=lambda row: row["name"].lower())
        return {
            "mode": "mafia",
            "winner": result.get("winner"),
            "roles": roles,
            "alive": result.get("alive", []),
        }
    return None


def make_unique_name(base: str, existing_names: List[str]) -> str:
    if base not in existing_names:
        return base
    suffix = 2
    while True:
        candidate = f"{base} ({suffix})"
        if candidate not in existing_names:
            return candidate
        suffix += 1


def find_pid_by_name(state: Dict[str, Any], name: str) -> Optional[str]:
    for pid, info in state.get("players", {}).items():
        if info.get("name") == name:
            return pid
    return None


def transfer_player_identity(state: Dict[str, Any], old_pid: str, new_pid: str) -> None:
    if old_pid == new_pid:
        return
    state["players"].pop(new_pid, None)
    state["scores"].pop(new_pid, None)
    state["submissions"].pop(new_pid, None)
    state["votebattle_entries"].pop(new_pid, None)
    state["votebattle_votes"].pop(new_pid, None)
    state["spyfall_roles"].pop(new_pid, None)
    state["mafia_wolf_votes"].pop(new_pid, None)
    state["mafia_day_votes"].pop(new_pid, None)
    state["mafia_seer_results"].pop(new_pid, None)
    state["steal_attempts"].pop(new_pid, None)

    if old_pid in state.get("players", {}):
        state["players"][new_pid] = state["players"].pop(old_pid)
    if old_pid in state.get("scores", {}):
        state["scores"][new_pid] = state["scores"].pop(old_pid, 0)
    if old_pid in state.get("teams", {}):
        state["teams"][new_pid] = state["teams"].pop(old_pid)
    if old_pid in state.get("submissions", {}):
        state["submissions"][new_pid] = state["submissions"].pop(old_pid)
    for voter, target in list(state.get("submissions", {}).items()):
        if target == old_pid:
            state["submissions"][voter] = new_pid
    if old_pid in state.get("votebattle_entries", {}):
        state["votebattle_entries"][new_pid] = state["votebattle_entries"].pop(old_pid)
    if old_pid in state.get("votebattle_votes", {}):
        state["votebattle_votes"][new_pid] = state["votebattle_votes"].pop(old_pid)
    if old_pid in state.get("spyfall_roles", {}):
        state["spyfall_roles"][new_pid] = state["spyfall_roles"].pop(old_pid)
    if state.get("buzz_winner_pid") == old_pid:
        state["buzz_winner_pid"] = new_pid
    if state.get("answer_pid") == old_pid:
        state["answer_pid"] = new_pid
    if old_pid in state.get("mafia_roles", {}):
        state["mafia_roles"][new_pid] = state["mafia_roles"].pop(old_pid)
    if old_pid in state.get("mafia_alive", []):
        state["mafia_alive"] = [new_pid if pid == old_pid else pid for pid in state.get("mafia_alive", [])]
    if state.get("spyfall_spy_pid") == old_pid:
        state["spyfall_spy_pid"] = new_pid
    if old_pid in state.get("mafia_wolf_votes", {}):
        state["mafia_wolf_votes"][new_pid] = state["mafia_wolf_votes"].pop(old_pid)
    for wolf, target in list(state.get("mafia_wolf_votes", {}).items()):
        if target == old_pid:
            state["mafia_wolf_votes"][wolf] = new_pid
    if old_pid in state.get("mafia_day_votes", {}):
        state["mafia_day_votes"][new_pid] = state["mafia_day_votes"].pop(old_pid)
    for voter, target in list(state.get("mafia_day_votes", {}).items()):
        if target == old_pid:
            state["mafia_day_votes"][voter] = new_pid
    if old_pid in state.get("mafia_seer_results", {}):
        state["mafia_seer_results"][new_pid] = state["mafia_seer_results"].pop(old_pid)
    for seer, result in list(state.get("mafia_seer_results", {}).items()):
        if isinstance(result, dict) and result.get("target") == old_pid:
            result["target"] = new_pid

    for entry in state.get("votebattle_order", []):
        if entry.get("pid") == old_pid:
            entry["pid"] = new_pid
    if old_pid in state.get("steal_attempts", {}):
        state["steal_attempts"][new_pid] = state["steal_attempts"].pop(old_pid)


def is_host_request() -> bool:
    if request.cookies.get("host") != HOST_KEY:
        return False
    if HOST_LOCALONLY and not is_local_request():
        return False
    return True


def register_routes(app: Flask) -> None:
    
    
    @app.get("/")
    def index() -> str:
        pid = request.cookies.get("pid")
        snapshot = get_state_snapshot()
        if pid and pid in snapshot.get("players", {}):
            return redirect(url_for("play"))
        error = request.args.get("error")
        return render_page(
            JOIN_BODY,
            title=APP_TITLE,
            body_class="player",
            app_title=APP_TITLE,
            error=error,
            require_lobby_code=snapshot.get("require_lobby_code", True),
            name_max_len=NAME_MAX_LEN,
        )
    
    
    @app.post("/join")
    def join() -> Any:
        name_raw = request.form.get("name") or ""
        name = clean_text_answer(name_raw, NAME_MAX_LEN)
        if not name:
            return redirect(url_for("index", error="Display name is required."))
        lobby_code_input = (request.form.get("lobby_code") or "").strip()
        conflict_action = request.form.get("conflict_action") or ""
        pid = request.cookies.get("pid") or str(uuid.uuid4())
    
        with STATE_LOCK:
            if pid not in STATE["players"] and STATE.get("lobby_locked"):
                return redirect(url_for("index", error="Lobby is locked."))
            if not validate_lobby_code(
                lobby_code_input,
                STATE.get("lobby_code", ""),
                STATE.get("require_lobby_code", True),
            ):
                return redirect(url_for("index", error="Invalid lobby code."))
            error = check_text_allowed(name, STATE)
            if error:
                return redirect(url_for("index", error=error))
    
            existing_names = [info.get("name", "") for info in STATE.get("players", {}).values()]
            existing_pid = find_pid_by_name(STATE, name)
            if existing_pid and existing_pid != pid:
                if conflict_action == "join_suffix":
                    name = make_unique_name(name, existing_names)
                elif conflict_action == "reclaim":
                    request_id = str(uuid.uuid4())
                    STATE["reclaim_requests"].append(
                        {
                            "request_id": request_id,
                            "name": name,
                            "new_pid": pid,
                            "ts": time.time(),
                        }
                    )
                    resp = make_response(redirect(url_for("reclaim_wait")))
                    resp.set_cookie("pid", pid, max_age=60 * 60 * 24 * 30, samesite="Lax", httponly=True)
                    return resp
                else:
                    suggested = make_unique_name(name, existing_names)
                    resp = make_response(
                        render_page(
                            NAME_CONFLICT_BODY,
                            title=f"{APP_TITLE} - Name Taken",
                            body_class="player",
                            app_title=APP_TITLE,
                            name=name,
                            suggested_name=suggested,
                            lobby_code=lobby_code_input,
                        )
                    )
                    resp.set_cookie("pid", pid, max_age=60 * 60 * 24 * 30, samesite="Lax", httponly=True)
                    return resp
    
            if pid not in STATE["players"]:
                STATE["players"][pid] = {"name": name}
                STATE["scores"][pid] = 0
                assign_team_for_new_player(STATE, pid)
            else:
                if not STATE.get("allow_renames", True) and name != STATE["players"][pid].get("name"):
                    return redirect(url_for("play", msg="Name changes are disabled."))
                STATE["players"][pid]["name"] = name
                if STATE.get("teams_enabled") and pid not in STATE.get("teams", {}):
                    assign_team_for_new_player(STATE, pid)
            if pid not in STATE["scores"]:
                STATE["scores"][pid] = 0
    
        resp = make_response(redirect(url_for("play")))
        resp.set_cookie("pid", pid, max_age=60 * 60 * 24 * 30, samesite="Lax", httponly=True)
        return resp
    
    
    @app.get("/reclaim")
    def reclaim_wait() -> Any:
        pid = request.cookies.get("pid")
        if not pid:
            return redirect(url_for("index"))
        with STATE_LOCK:
            notice = STATE.get("reclaim_notices", {}).pop(pid, None)
            if pid in STATE.get("players", {}):
                if notice:
                    return redirect(url_for("play", msg=notice))
                return redirect(url_for("play"))
        return render_page(
            RECLAIM_WAIT_BODY,
            title=f"{APP_TITLE} - Reclaim",
            body_class="player",
            app_title=APP_TITLE,
        )
    
    
    @app.get("/play")
    def play() -> str:
        pid = request.cookies.get("pid")
        snapshot = get_state_snapshot()
        player = snapshot.get("players", {}).get(pid or "")
        if not player:
            return redirect(url_for("index"))
        mode = snapshot.get("mode")
        phase = snapshot.get("phase")
        votebattle_phase = snapshot.get("votebattle_phase")
        spyfall_phase = snapshot.get("spyfall_phase")
        mafia_phase = snapshot.get("mafia_phase")
        if mode == "votebattle":
            if votebattle_phase == "vote":
                submitted = pid in snapshot.get("votebattle_votes", {})
            else:
                submitted = pid in snapshot.get("votebattle_entries", {})
        elif mode == "spyfall" and spyfall_phase == "vote":
            submitted = pid in snapshot.get("submissions", {})
        elif mode == "mafia":
            if mafia_phase == "night":
                role = snapshot.get("mafia_roles", {}).get(pid)
                if role == "werewolf":
                    submitted = pid in snapshot.get("mafia_wolf_votes", {})
                elif role == "seer":
                    submitted = pid in snapshot.get("mafia_seer_results", {})
                else:
                    submitted = False
            elif mafia_phase == "day":
                submitted = pid in snapshot.get("mafia_day_votes", {})
            else:
                submitted = False
        else:
            submitted = pid in snapshot.get("submissions", {})
        player_choices = []
        for player_id, info in snapshot.get("players", {}).items():
            player_choices.append({"pid": player_id, "name": info.get("name", "Unknown")})
        player_choices.sort(key=lambda row: row["name"].lower())
        results_view = build_results_view(snapshot, reveal_authors=False) if snapshot.get("phase") == "revealed" else None
        scoreboard = get_scoreboard(snapshot.get("players", {}), snapshot.get("scores", {}))
        message = request.args.get("msg")
        votebattle_choices = []
        if mode == "votebattle" and votebattle_phase == "vote":
            votebattle_choices = build_votebattle_choices(snapshot, pid)
        alive_players = []
        mafia_alive = snapshot.get("mafia_alive", [])
        mafia_alive_set = set(mafia_alive)
        for player_id, info in snapshot.get("players", {}).items():
            if player_id in mafia_alive_set:
                alive_players.append({"pid": player_id, "name": info.get("name", "Unknown")})
        alive_players.sort(key=lambda row: row["name"].lower())
        mafia_role = snapshot.get("mafia_roles", {}).get(pid)
        seer_result = None
        raw_seer_result = snapshot.get("mafia_seer_results", {}).get(pid)
        if isinstance(raw_seer_result, dict):
            target_pid = raw_seer_result.get("target")
            target_name = snapshot.get("players", {}).get(target_pid, {}).get("name", "Unknown")
            seer_result = {
                "target_name": target_name,
                "is_werewolf": bool(raw_seer_result.get("is_werewolf")),
            }
        last_eliminated_pid = snapshot.get("mafia_last_eliminated")
        last_eliminated_name = None
        if last_eliminated_pid:
            last_eliminated_name = snapshot.get("players", {}).get(last_eliminated_pid, {}).get("name", "Unknown")
        trivia_phase = snapshot.get("trivia_buzzer_phase")
        buzz_winner_pid = snapshot.get("buzz_winner_pid")
        buzz_winner_name = (
            snapshot.get("players", {}).get(buzz_winner_pid, {}).get("name", "Unknown") if buzz_winner_pid else ""
        )
        buzz_winner_team_id = snapshot.get("buzz_winner_team_id")
        buzz_winner_team_label = snapshot.get("team_names", {}).get(buzz_winner_team_id, "") if buzz_winner_team_id else ""
        answer_pid = snapshot.get("answer_pid")
        answer_name = snapshot.get("players", {}).get(answer_pid, {}).get("name", "Unknown") if answer_pid else ""
        answer_team_id = snapshot.get("answer_team_id")
        answer_team_label = snapshot.get("team_names", {}).get(answer_team_id, "") if answer_team_id else ""
        player_team_id = snapshot.get("teams", {}).get(pid)
        is_team_mode = mode == "team_trivia"
        answer_choice = snapshot.get("answer_choice")
        steal_attempts = snapshot.get("steal_attempts", {})
        has_steal_attempt = pid in steal_attempts
        answer_locked = answer_choice is not None
        can_buzz = trivia_phase == "buzz" and not buzz_winner_pid
        if is_team_mode:
            can_answer = (
                trivia_phase == "answer"
                and not answer_locked
                and player_team_id is not None
                and player_team_id == buzz_winner_team_id
            )
            can_steal = (
                trivia_phase == "steal"
                and not has_steal_attempt
                and player_team_id is not None
                and player_team_id != buzz_winner_team_id
            )
        else:
            can_answer = trivia_phase == "answer" and not answer_locked and pid == buzz_winner_pid
            can_steal = trivia_phase == "steal" and not has_steal_attempt and pid != buzz_winner_pid
        return render_page(
            PLAY_BODY,
            title=f"{APP_TITLE} - Play",
            body_class="player",
            player_name=player.get("name", "Player"),
            team_label=get_team_label(snapshot, pid),
            pid=pid,
            is_spy=pid == snapshot.get("spyfall_spy_pid"),
            mode=mode,
            mode_label=label_for_mode(mode or ""),
            phase=phase,
            prompt=snapshot.get("prompt", ""),
            options=snapshot.get("options", []),
            round_id=snapshot.get("round_id", 0),
            submitted=submitted,
            player_choices=player_choices,
            alive_choices=alive_players,
            results=results_view,
            scoreboard=scoreboard,
            team_scoreboard=get_team_scoreboard(snapshot),
            message=message,
            public_phase=phase,
            public_mode=mode,
            public_round_id=snapshot.get("round_id", 0),
            public_votebattle_phase=votebattle_phase,
            public_spyfall_phase=spyfall_phase,
            public_mafia_phase=mafia_phase,
            public_trivia_buzzer_phase=snapshot.get("trivia_buzzer_phase"),
            public_poll_ms=PUBLIC_POLL_MS,
            text_max_len=TEXT_MAX_LEN,
            quickdraw_max_len=QUICKDRAW_MAX_LEN,
            votebattle_max_len=VOTEBATTLE_MAX_LEN,
            votebattle_phase=votebattle_phase,
            votebattle_choices=votebattle_choices,
            submissions_locked=snapshot.get("submissions_locked", False),
            timer_remaining=get_timer_remaining(snapshot),
            spyfall_phase=spyfall_phase,
            spyfall_location=snapshot.get("spyfall_location", ""),
            spyfall_spy_pid=snapshot.get("spyfall_spy_pid"),
            spyfall_role=snapshot.get("spyfall_roles", {}).get(pid),
            mafia_phase=mafia_phase,
            mafia_role=mafia_role,
            mafia_alive=pid in mafia_alive_set,
            mafia_last_eliminated=last_eliminated_name,
            seer_result=seer_result,
            trivia_buzzer_phase=snapshot.get("trivia_buzzer_phase"),
            buzz_winner_pid=snapshot.get("buzz_winner_pid"),
            buzz_winner_team_id=snapshot.get("buzz_winner_team_id"),
            answer_pid=snapshot.get("answer_pid"),
            answer_team_id=snapshot.get("answer_team_id"),
            answer_choice=snapshot.get("answer_choice"),
            steal_attempts=snapshot.get("steal_attempts", {}),
            trivia_buzzer_steal_enabled=snapshot.get("trivia_buzzer_steal_enabled", True),
            buzz_winner_name=buzz_winner_name,
            buzz_winner_team_label=buzz_winner_team_label,
            answer_name=answer_name,
            answer_team_label=answer_team_label,
            is_team_mode=is_team_mode,
            can_buzz=can_buzz,
            can_answer=can_answer,
            can_steal=can_steal,
            answer_locked=answer_locked,
            has_steal_attempt=has_steal_attempt,
            option_labels=["A", "B", "C", "D"],
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
            if STATE.get("submissions_locked"):
                return redirect(url_for("play", msg="Submissions are locked."))
    
            mode = STATE["mode"]
            if mode == "spyfall":
                if STATE.get("spyfall_phase") != "vote":
                    return redirect(url_for("play", msg="Voting is not active."))
                if pid in STATE.get("submissions", {}):
                    return redirect(url_for("play", msg="Already voted."))
                target = request.form.get("vote")
                if target not in STATE["players"]:
                    return redirect(url_for("play", msg="Invalid selection."))
                if target == pid and not STATE.get("spyfall_allow_self_vote", False):
                    return redirect(url_for("play", msg="You cannot vote for yourself."))
                STATE["submissions"][pid] = target
                return redirect(url_for("play"))
    
            if mode == "mafia":
                mafia_phase = STATE.get("mafia_phase")
                alive = set(STATE.get("mafia_alive", []))
                if pid not in alive:
                    return redirect(url_for("play", msg="You have been eliminated."))
                role = STATE.get("mafia_roles", {}).get(pid)
                if mafia_phase == "night":
                    if role == "werewolf":
                        if pid in STATE.get("mafia_wolf_votes", {}):
                            return redirect(url_for("play", msg="Already submitted."))
                        target = request.form.get("wolf_target")
                        if target not in alive or target == pid:
                            return redirect(url_for("play", msg="Invalid target."))
                        STATE.setdefault("mafia_wolf_votes", {})[pid] = target
                        return redirect(url_for("play"))
                    if role == "seer":
                        if pid in STATE.get("mafia_seer_results", {}):
                            return redirect(url_for("play", msg="Already submitted."))
                        target = request.form.get("seer_target")
                        if target not in alive or target == pid:
                            return redirect(url_for("play", msg="Invalid target."))
                        is_werewolf = STATE.get("mafia_roles", {}).get(target) == "werewolf"
                        STATE.setdefault("mafia_seer_results", {})[pid] = {"target": target, "is_werewolf": is_werewolf}
                        return redirect(url_for("play"))
                    return redirect(url_for("play", msg="You are asleep."))
                if mafia_phase == "day":
                    if pid in STATE.get("mafia_day_votes", {}):
                        return redirect(url_for("play", msg="Already voted."))
                    target = request.form.get("vote")
                    if target not in alive:
                        return redirect(url_for("play", msg="Invalid selection."))
                    STATE.setdefault("mafia_day_votes", {})[pid] = target
                    return redirect(url_for("play"))
                return redirect(url_for("play", msg="Voting is not active."))

            if mode in ("trivia_buzzer", "team_trivia"):
                trivia_phase = STATE.get("trivia_buzzer_phase")
                if trivia_phase == "buzz":
                    if STATE.get("buzz_winner_pid"):
                        return redirect(url_for("play", msg="Buzz already locked."))
                    if request.form.get("buzz") != "1":
                        return redirect(url_for("play", msg="Buzz not received."))
                    if mode == "team_trivia":
                        team_id = STATE.get("teams", {}).get(pid)
                        if team_id is None:
                            return redirect(url_for("play", msg="Team not assigned."))
                    ts = time.time()
                    winner_pid, winner_ts = select_buzz_winner(
                        STATE.get("buzz_winner_pid"),
                        STATE.get("buzz_ts"),
                        pid,
                        ts,
                    )
                    STATE["buzz_winner_pid"] = winner_pid
                    STATE["buzz_ts"] = winner_ts
                    if mode == "team_trivia":
                        STATE["buzz_winner_team_id"] = team_id
                    return redirect(url_for("play"))

                if trivia_phase == "answer":
                    if STATE.get("answer_choice") is not None:
                        return redirect(url_for("play", msg="Answer already submitted."))
                    if mode == "team_trivia":
                        team_id = STATE.get("teams", {}).get(pid)
                        if team_id is None or team_id != STATE.get("buzz_winner_team_id"):
                            return redirect(url_for("play", msg="Your team did not buzz."))
                    else:
                        if pid != STATE.get("buzz_winner_pid"):
                            return redirect(url_for("play", msg="Only the buzz winner can answer."))
                    choice_raw = request.form.get("choice", "")
                    try:
                        choice = int(choice_raw)
                    except ValueError:
                        return redirect(url_for("play", msg="Invalid selection."))
                    if choice < 0 or choice >= len(STATE.get("options", [])):
                        return redirect(url_for("play", msg="Invalid selection."))
                    STATE["answer_choice"] = choice
                    STATE["answer_pid"] = pid
                    if mode == "team_trivia":
                        STATE["answer_team_id"] = STATE.get("teams", {}).get(pid)
                    return redirect(url_for("play"))

                if trivia_phase == "steal":
                    if mode == "team_trivia":
                        team_id = STATE.get("teams", {}).get(pid)
                        if team_id is None or team_id == STATE.get("buzz_winner_team_id"):
                            return redirect(url_for("play", msg="Your team cannot steal."))
                    else:
                        if pid == STATE.get("buzz_winner_pid"):
                            return redirect(url_for("play", msg="Buzz winner cannot steal."))
                    if pid in STATE.get("steal_attempts", {}):
                        return redirect(url_for("play", msg="Already attempted to steal."))
                    choice_raw = request.form.get("choice", "")
                    try:
                        choice = int(choice_raw)
                    except ValueError:
                        return redirect(url_for("play", msg="Invalid selection."))
                    if choice < 0 or choice >= len(STATE.get("options", [])):
                        return redirect(url_for("play", msg="Invalid selection."))
                    STATE.setdefault("steal_attempts", {})[pid] = choice
                    return redirect(url_for("play"))

                return redirect(url_for("play", msg="Buzzer phase is not active."))

            if mode == "votebattle":
                votebattle_phase = STATE.get("votebattle_phase")
                if votebattle_phase == "submit":
                    if pid in STATE.get("votebattle_entries", {}):
                        return redirect(url_for("play", msg="Already submitted."))
                    text_raw = request.form.get("votebattle_text", "")
                    text = clean_text_answer(text_raw, VOTEBATTLE_MAX_LEN)
                    if not text:
                        return redirect(url_for("play", msg="Entry cannot be empty."))
                    error = check_text_allowed(text, STATE)
                    if error:
                        return redirect(url_for("play", msg=error))
                    STATE["votebattle_entries"][pid] = text
                    entry_id = STATE.get("votebattle_counter", 0)
                    STATE["votebattle_counter"] = entry_id + 1
                    STATE["votebattle_order"].append({"id": entry_id, "pid": pid, "text": text})
                elif votebattle_phase == "vote":
                    if pid in STATE.get("votebattle_votes", {}):
                        return redirect(url_for("play", msg="Already voted."))
                    choice_raw = request.form.get("votebattle_vote", "")
                    try:
                        entry_id = int(choice_raw)
                    except ValueError:
                        return redirect(url_for("play", msg="Invalid selection."))
                    order = STATE.get("votebattle_order", [])
                    entry = next((item for item in order if item.get("id") == entry_id), None)
                    if entry is None:
                        return redirect(url_for("play", msg="Invalid selection."))
                    if entry.get("pid") == pid:
                        return redirect(url_for("play", msg="You cannot vote for your own entry."))
                    STATE["votebattle_votes"][pid] = entry_id
                else:
                    return redirect(url_for("play", msg="Voting is not active."))
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
            elif mode == "hotseat":
                text_raw = request.form.get("text_answer", "")
                text = clean_text_answer(text_raw, TEXT_MAX_LEN)
                if not text:
                    return redirect(url_for("play", msg="Answer cannot be empty."))
                error = check_text_allowed(text, STATE)
                if error:
                    return redirect(url_for("play", msg=error))
                STATE["submissions"][pid] = text
            elif mode == "quickdraw":
                text_raw = request.form.get("text_answer", "")
                text = clean_text_answer(text_raw, QUICKDRAW_MAX_LEN)
                if not text:
                    return redirect(url_for("play", msg="Answer cannot be empty."))
                error = check_text_allowed(text, STATE)
                if error:
                    return redirect(url_for("play", msg=error))
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
            players.append(
                {
                    "pid": pid,
                    "name": info.get("name", "Unknown"),
                    "team": get_team_label(snapshot, pid),
                }
            )
        players.sort(key=lambda row: row["name"].lower())
        scoreboard = get_scoreboard(snapshot.get("players", {}), snapshot.get("scores", {}))
        team_scoreboard = get_team_scoreboard(snapshot)
        results_view = build_results_view(snapshot, reveal_authors=True) if snapshot.get("phase") == "revealed" else None
        submission_count = get_active_submission_count(snapshot)
        submission_names = get_active_submission_names(snapshot)
        submission_target = get_submission_target_count(snapshot)
        progress_percent = int((submission_count / submission_target) * 100) if submission_target else 0
        mode = snapshot.get("mode", "")
        phase = snapshot.get("phase", "")
        votebattle_phase = snapshot.get("votebattle_phase")
        spyfall_phase = snapshot.get("spyfall_phase")
        mafia_phase = snapshot.get("mafia_phase")
        show_prompt_control = mode in (
            "mlt",
            "wyr",
            "trivia",
            "trivia_buzzer",
            "team_trivia",
            "hotseat",
            "quickdraw",
            "wavelength",
            "votebattle",
            "spyfall",
        )
        show_game_settings_quickdraw = mode == "quickdraw"
        show_game_settings_wyr = mode == "wyr"
        show_game_settings_spyfall = mode == "spyfall"
        show_game_settings_mafia = mode == "mafia"
        show_game_settings_buzzer = mode in ("trivia_buzzer", "team_trivia")
        show_progress_button, progress_label = get_progress_ui(
            mode,
            phase,
            votebattle_phase,
            spyfall_phase,
            mafia_phase,
            snapshot.get("trivia_buzzer_phase"),
        )
        show_reveal_button = mode not in ("votebattle", "spyfall", "mafia", "trivia_buzzer", "team_trivia")
        votebattle_submit_count = len(snapshot.get("votebattle_entries", {}))
        votebattle_vote_count = len(snapshot.get("votebattle_votes", {}))
        reclaim_requests = []
        for req in snapshot.get("reclaim_requests", []):
            reclaim_requests.append(
                {
                    "request_id": req.get("request_id"),
                    "name": req.get("name", "Unknown"),
                    "ts": req.get("ts", 0),
                }
            )
        buzz_winner_pid = snapshot.get("buzz_winner_pid")
        buzz_winner_name = snapshot.get("players", {}).get(buzz_winner_pid, {}).get("name") if buzz_winner_pid else ""
        buzz_team_id = snapshot.get("buzz_winner_team_id")
        buzz_team_label = snapshot.get("team_names", {}).get(buzz_team_id) if buzz_team_id else ""
        buzz_winner_display = (
            f"{buzz_winner_name} ({buzz_team_label})"
            if buzz_winner_name and buzz_team_label
            else buzz_winner_name
            if buzz_winner_name
            else "--"
        )
        answer_pid = snapshot.get("answer_pid")
        answer_name = snapshot.get("players", {}).get(answer_pid, {}).get("name") if answer_pid else ""
        answer_team_id = snapshot.get("answer_team_id")
        answer_team_label = snapshot.get("team_names", {}).get(answer_team_id) if answer_team_id else ""
        answer_display = (
            f"{answer_name} ({answer_team_label})"
            if answer_name and answer_team_label
            else answer_name
            if answer_name
            else "--"
        )
        return render_page(
            HOST_BODY,
            title=f"{APP_TITLE} - Host",
            body_class="host",
            player_count=len(snapshot.get("players", {})),
            submission_count=submission_count,
            submission_target=submission_target,
            progress_percent=progress_percent,
            mode=mode,
            mode_label=label_for_mode(mode),
            phase=phase,
            phase_label=label_for_phase(phase),
            round_id=snapshot.get("round_id", 0),
            prompt=snapshot.get("prompt", ""),
            options=snapshot.get("options", []),
            correct_index=snapshot.get("correct_index"),
            wavelength_target=snapshot.get("wavelength_target"),
            votebattle_phase=votebattle_phase,
            votebattle_submit_count=votebattle_submit_count,
            votebattle_vote_count=votebattle_vote_count,
            spyfall_phase=spyfall_phase,
            mafia_phase=mafia_phase,
            trivia_buzzer_phase=snapshot.get("trivia_buzzer_phase"),
            buzz_winner_display=buzz_winner_display,
            answer_display=answer_display,
            submission_names=submission_names,
            players=players,
            scoreboard=scoreboard,
            team_scoreboard=team_scoreboard,
            results=results_view,
            host_message=snapshot.get("host_message", ""),
            lobby_locked=snapshot.get("lobby_locked", False),
            allow_renames=snapshot.get("allow_renames", True),
            quickdraw_scoring=snapshot.get("quickdraw_scoring", "unique"),
            prompt_mode=snapshot.get("prompt_mode", "random"),
            manual_prompt_text=snapshot.get("manual_prompt_text", ""),
            manual_wyr_a=snapshot.get("manual_wyr_a", ""),
            manual_wyr_b=snapshot.get("manual_wyr_b", ""),
            manual_trivia_0=snapshot.get("manual_trivia_0", ""),
            manual_trivia_1=snapshot.get("manual_trivia_1", ""),
            manual_trivia_2=snapshot.get("manual_trivia_2", ""),
            manual_trivia_3=snapshot.get("manual_trivia_3", ""),
            manual_correct_index=snapshot.get("manual_correct_index"),
            manual_wavelength_target=snapshot.get("manual_wavelength_target"),
            manual_wavelength_target_enabled=snapshot.get("manual_wavelength_target_enabled", False),
            openai_enabled=openai_ready(),
            mode_labels=MODE_LABELS,
            mode_descriptions=MODE_DESCRIPTIONS,
            wyr_points_majority=snapshot.get("wyr_points_majority", False),
            show_prompt_control=show_prompt_control,
            show_game_settings_quickdraw=show_game_settings_quickdraw,
            show_game_settings_wyr=show_game_settings_wyr,
            show_game_settings_spyfall=show_game_settings_spyfall,
            show_game_settings_mafia=show_game_settings_mafia,
            show_game_settings_buzzer=show_game_settings_buzzer,
            show_progress_button=show_progress_button,
            progress_label=progress_label,
            show_reveal_button=show_reveal_button,
            spyfall_auto_start_vote_on_timer=snapshot.get("spyfall_auto_start_vote_on_timer", True),
            spyfall_allow_self_vote=snapshot.get("spyfall_allow_self_vote", False),
            mafia_seer_enabled=snapshot.get("mafia_seer_enabled", True),
            mafia_auto_wolf_count=snapshot.get("mafia_auto_wolf_count", True),
            mafia_wolf_count=snapshot.get("mafia_wolf_count", 1),
            mafia_reveal_roles_on_end=snapshot.get("mafia_reveal_roles_on_end", True),
            trivia_buzzer_steal_enabled=snapshot.get("trivia_buzzer_steal_enabled", True),
            join_url=join_url,
            host_url=host_url,
            join_qr_data=join_qr_data,
            lobby_code=snapshot.get("lobby_code", ""),
            require_lobby_code=snapshot.get("require_lobby_code", True),
            teams_enabled=snapshot.get("teams_enabled", False),
            team_count=snapshot.get("team_count", 2),
            team_names=snapshot.get("team_names", {}),
            filter_mode=snapshot.get("filter_mode", "mild"),
            openai_moderation_enabled=snapshot.get("openai_moderation_enabled", False),
            timer_enabled=snapshot.get("timer_enabled", False),
            timer_seconds=snapshot.get("timer_seconds", TIMER_DEFAULT_SECONDS),
            vote_timer_seconds=snapshot.get("vote_timer_seconds", VOTE_TIMER_DEFAULT_SECONDS),
            auto_advance=snapshot.get("auto_advance", True),
            late_submit_policy=snapshot.get("late_submit_policy", "lock_after_timer"),
            timer_remaining=get_timer_remaining(snapshot),
            submissions_locked=snapshot.get("submissions_locked", False),
            reclaim_requests=reclaim_requests,
            host_poll_ms=HOST_POLL_MS,
            host_timer_poll_ms=HOST_TIMER_POLL_MS,
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
                    reset_pool(STATE, "mlt")
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
                    reset_pool(STATE, "wyr")
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
                    reset_pool(STATE, "trivia")
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
                    reset_pool(STATE, "hotseat")
                    STATE["host_message"] = f"Generated {len(prompts)} hot seat prompts."
                else:
                    STATE["host_message"] = err or "Failed to generate hot seat prompts."
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
                    reset_pool(STATE, "wavelength")
                    STATE["host_message"] = f"Generated {len(prompts)} wavelength prompts."
                else:
                    STATE["host_message"] = err or "Failed to generate wavelength prompts."
            return redirect(url_for("host"))
    
        if action == "generate_quickdraw":
            if not openai_ready():
                with STATE_LOCK:
                    STATE["host_message"] = "OpenAI is not configured."
                return redirect(url_for("host"))
            prompts, err = generate_quickdraw_prompts()
            with STATE_LOCK:
                if prompts:
                    global QUICKDRAW_PROMPTS
                    QUICKDRAW_PROMPTS = prompts
                    reset_pool(STATE, "quickdraw")
                    STATE["host_message"] = f"Generated {len(prompts)} quick draw prompts."
                else:
                    STATE["host_message"] = err or "Failed to generate quick draw prompts."
            return redirect(url_for("host"))
    
        if action == "generate_votebattle":
            if not openai_ready():
                with STATE_LOCK:
                    STATE["host_message"] = "OpenAI is not configured."
                return redirect(url_for("host"))
            prompts, err = generate_votebattle_prompts()
            with STATE_LOCK:
                if prompts:
                    global VOTEBATTLE_PROMPTS
                    VOTEBATTLE_PROMPTS = prompts
                    reset_pool(STATE, "votebattle")
                    STATE["host_message"] = f"Generated {len(prompts)} vote battle prompts."
                else:
                    STATE["host_message"] = err or "Failed to generate vote battle prompts."
            return redirect(url_for("host"))
    
        if action == "download_recap":
            with STATE_LOCK:
                payload = build_recap_payload(STATE)
            resp = make_response(json.dumps(payload, indent=2))
            resp.headers["Content-Type"] = "application/json"
            resp.headers["Content-Disposition"] = "attachment; filename=party_recap.json"
            return resp
    
        with STATE_LOCK:
            STATE["host_message"] = ""
            if action == "progress":
                resolved = resolve_progress_action(
                    STATE.get("mode", ""),
                    STATE.get("phase", ""),
                    STATE.get("votebattle_phase"),
                    STATE.get("spyfall_phase"),
                    STATE.get("mafia_phase"),
                    STATE.get("trivia_buzzer_phase"),
                )
                if not resolved:
                    STATE["host_message"] = "No progress available."
                    return redirect(url_for("host"))
                action = resolved
            if action == "set_mode":
                mode = request.form.get("mode", "mlt")
                if STATE["phase"] == "in_round":
                    STATE["host_message"] = "Cannot change mode during an active round."
                elif mode in MODE_LABELS:
                    STATE["mode"] = mode
                    STATE["votebattle_phase"] = None
                    STATE["votebattle_entries"] = {}
                    STATE["votebattle_votes"] = {}
                    STATE["votebattle_order"] = []
                    STATE["votebattle_counter"] = 0
                    STATE["spyfall_phase"] = None
                    STATE["spyfall_location"] = ""
                    STATE["spyfall_spy_pid"] = None
                    STATE["spyfall_roles"] = {}
                    STATE["trivia_buzzer_phase"] = None
                    STATE["trivia_buzzer_question"] = ""
                    STATE["trivia_buzzer_options"] = []
                    STATE["trivia_buzzer_correct_index"] = None
                    STATE["buzz_winner_pid"] = None
                    STATE["buzz_winner_team_id"] = None
                    STATE["buzz_ts"] = None
                    STATE["answer_pid"] = None
                    STATE["answer_team_id"] = None
                    STATE["answer_choice"] = None
                    STATE["steal_attempts"] = {}
                    STATE["trivia_buzzer_result"] = None
                    STATE["mafia_phase"] = None
                    STATE["mafia_roles"] = {}
                    STATE["mafia_alive"] = []
                    STATE["mafia_wolf_votes"] = {}
                    STATE["mafia_day_votes"] = {}
                    STATE["mafia_seer_results"] = {}
                    STATE["mafia_last_eliminated"] = None
                    STATE["submissions_locked"] = False
                    stop_timer_locked(STATE)
                    STATE["host_message"] = f"Mode set to {label_for_mode(mode)}."
                else:
                    STATE["host_message"] = "Unknown mode."
    
            elif action == "start_round":
                if STATE["phase"] == "in_round":
                    STATE["host_message"] = "Round already in progress."
                elif not STATE["players"]:
                    STATE["host_message"] = "No players yet."
                else:
                    if start_new_round_locked(STATE["mode"]):
                        STATE["host_message"] = "Round started."
    
            elif action == "reveal":
                if STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round to reveal."
                elif STATE["mode"] == "votebattle" and STATE.get("votebattle_phase") != "vote":
                    STATE["host_message"] = "Start vote battle voting before revealing."
                elif STATE["mode"] == "spyfall" and STATE.get("spyfall_phase") != "vote":
                    STATE["host_message"] = "Start spy voting before revealing."
                elif STATE["mode"] == "mafia" and STATE.get("mafia_phase") != "over":
                    STATE["host_message"] = "Finish the mafia game before revealing."
                else:
                    compute_results_locked()
                    STATE["phase"] = "revealed"
                    STATE["submissions_locked"] = True
                    STATE["host_message"] = "Results revealed."
    
            elif action == "next_round":
                if STATE["phase"] == "in_round":
                    STATE["host_message"] = "Reveal results before starting next round."
                elif not STATE["players"]:
                    STATE["host_message"] = "No players yet."
                else:
                    if start_new_round_locked(STATE["mode"]):
                        STATE["host_message"] = "Next round started."
    
            elif action == "reset_round":
                STATE["phase"] = "lobby"
                STATE["prompt"] = ""
                STATE["options"] = []
                STATE["correct_index"] = None
                STATE["trivia_buzzer_phase"] = None
                STATE["trivia_buzzer_question"] = ""
                STATE["trivia_buzzer_options"] = []
                STATE["trivia_buzzer_correct_index"] = None
                STATE["buzz_winner_pid"] = None
                STATE["buzz_winner_team_id"] = None
                STATE["buzz_ts"] = None
                STATE["answer_pid"] = None
                STATE["answer_team_id"] = None
                STATE["answer_choice"] = None
                STATE["steal_attempts"] = {}
                STATE["trivia_buzzer_result"] = None
                STATE["wavelength_target"] = None
                STATE["submissions"] = {}
                STATE["submissions_locked"] = False
                STATE["votebattle_phase"] = None
                STATE["votebattle_entries"] = {}
                STATE["votebattle_votes"] = {}
                STATE["votebattle_order"] = []
                STATE["votebattle_counter"] = 0
                STATE["spyfall_phase"] = None
                STATE["spyfall_location"] = ""
                STATE["spyfall_spy_pid"] = None
                STATE["spyfall_roles"] = {}
                STATE["mafia_phase"] = None
                STATE["mafia_roles"] = {}
                STATE["mafia_alive"] = []
                STATE["mafia_wolf_votes"] = {}
                STATE["mafia_day_votes"] = {}
                STATE["mafia_seer_results"] = {}
                STATE["mafia_last_eliminated"] = None
                STATE["round_start_ts"] = None
                stop_timer_locked(STATE)
                STATE["last_result"] = None
                STATE["host_message"] = "Round reset."
    
            elif action == "reset_scores":
                for pid in list(STATE["scores"].keys()):
                    STATE["scores"][pid] = 0
                STATE["phase"] = "lobby"
                STATE["prompt"] = ""
                STATE["options"] = []
                STATE["correct_index"] = None
                STATE["trivia_buzzer_phase"] = None
                STATE["trivia_buzzer_question"] = ""
                STATE["trivia_buzzer_options"] = []
                STATE["trivia_buzzer_correct_index"] = None
                STATE["buzz_winner_pid"] = None
                STATE["buzz_winner_team_id"] = None
                STATE["buzz_ts"] = None
                STATE["answer_pid"] = None
                STATE["answer_team_id"] = None
                STATE["answer_choice"] = None
                STATE["steal_attempts"] = {}
                STATE["trivia_buzzer_result"] = None
                STATE["wavelength_target"] = None
                STATE["submissions"] = {}
                STATE["submissions_locked"] = False
                STATE["votebattle_phase"] = None
                STATE["votebattle_entries"] = {}
                STATE["votebattle_votes"] = {}
                STATE["votebattle_order"] = []
                STATE["votebattle_counter"] = 0
                STATE["spyfall_phase"] = None
                STATE["spyfall_location"] = ""
                STATE["spyfall_spy_pid"] = None
                STATE["spyfall_roles"] = {}
                STATE["mafia_phase"] = None
                STATE["mafia_roles"] = {}
                STATE["mafia_alive"] = []
                STATE["mafia_wolf_votes"] = {}
                STATE["mafia_day_votes"] = {}
                STATE["mafia_seer_results"] = {}
                STATE["mafia_last_eliminated"] = None
                STATE["round_start_ts"] = None
                stop_timer_locked(STATE)
                STATE["last_result"] = None
                STATE["host_message"] = "Scores reset."
    
            elif action == "kick":
                pid = request.form.get("pid")
                if pid and pid in STATE["players"]:
                    STATE["players"].pop(pid, None)
                    STATE["scores"].pop(pid, None)
                    STATE["submissions"].pop(pid, None)
                    STATE["votebattle_entries"].pop(pid, None)
                    STATE["votebattle_votes"].pop(pid, None)
                    STATE.get("steal_attempts", {}).pop(pid, None)
                    STATE.get("teams", {}).pop(pid, None)
                    STATE.get("spyfall_roles", {}).pop(pid, None)
                    if STATE.get("spyfall_spy_pid") == pid:
                        STATE["spyfall_spy_pid"] = None
                    if STATE.get("buzz_winner_pid") == pid:
                        STATE["buzz_winner_pid"] = None
                        STATE["buzz_ts"] = None
                        STATE["buzz_winner_team_id"] = None
                    if STATE.get("answer_pid") == pid:
                        STATE["answer_pid"] = None
                        STATE["answer_choice"] = None
                        STATE["answer_team_id"] = None
                    STATE.get("mafia_roles", {}).pop(pid, None)
                    STATE["mafia_alive"] = [alive for alive in STATE.get("mafia_alive", []) if alive != pid]
                    STATE.get("mafia_wolf_votes", {}).pop(pid, None)
                    STATE.get("mafia_day_votes", {}).pop(pid, None)
                    STATE.get("mafia_seer_results", {}).pop(pid, None)
                    removed_ids = {entry.get("id") for entry in STATE["votebattle_order"] if entry.get("pid") == pid}
                    STATE["votebattle_order"] = [entry for entry in STATE["votebattle_order"] if entry.get("pid") != pid]
                    if removed_ids:
                        STATE["votebattle_votes"] = {
                            voter: vote for voter, vote in STATE["votebattle_votes"].items() if vote not in removed_ids
                        }
                    STATE["host_message"] = "Player removed."
                else:
                    STATE["host_message"] = "Player not found."
    
            elif action == "kick_all":
                STATE["players"] = {}
                STATE["scores"] = {}
                STATE["teams"] = {}
                STATE["submissions"] = {}
                STATE["phase"] = "lobby"
                STATE["prompt"] = ""
                STATE["options"] = []
                STATE["correct_index"] = None
                STATE["trivia_buzzer_phase"] = None
                STATE["trivia_buzzer_question"] = ""
                STATE["trivia_buzzer_options"] = []
                STATE["trivia_buzzer_correct_index"] = None
                STATE["buzz_winner_pid"] = None
                STATE["buzz_winner_team_id"] = None
                STATE["buzz_ts"] = None
                STATE["answer_pid"] = None
                STATE["answer_team_id"] = None
                STATE["answer_choice"] = None
                STATE["steal_attempts"] = {}
                STATE["trivia_buzzer_result"] = None
                STATE["wavelength_target"] = None
                STATE["submissions_locked"] = False
                STATE["votebattle_phase"] = None
                STATE["votebattle_entries"] = {}
                STATE["votebattle_votes"] = {}
                STATE["votebattle_order"] = []
                STATE["votebattle_counter"] = 0
                STATE["spyfall_phase"] = None
                STATE["spyfall_location"] = ""
                STATE["spyfall_spy_pid"] = None
                STATE["spyfall_roles"] = {}
                STATE["mafia_phase"] = None
                STATE["mafia_roles"] = {}
                STATE["mafia_alive"] = []
                STATE["mafia_wolf_votes"] = {}
                STATE["mafia_day_votes"] = {}
                STATE["mafia_seer_results"] = {}
                STATE["mafia_last_eliminated"] = None
                STATE["round_start_ts"] = None
                stop_timer_locked(STATE)
                STATE["last_result"] = None
                STATE["round_id"] = 0
                STATE["reclaim_requests"] = []
                STATE["reclaim_notices"] = {}
                STATE["host_message"] = "All players removed."
    
            elif action == "set_wyr_points":
                STATE["wyr_points_majority"] = request.form.get("points_majority") == "on"
                STATE["host_message"] = "WYR scoring updated."
    
            elif action == "set_quickdraw_scoring":
                scoring = request.form.get("quickdraw_scoring", "unique")
                if scoring not in ("unique", "host"):
                    scoring = "unique"
                STATE["quickdraw_scoring"] = scoring
                STATE["host_message"] = "Quick Draw scoring updated."

            elif action == "set_trivia_buzzer_settings":
                steal_enabled = request.form.get("steal_enabled") == "on"
                STATE["trivia_buzzer_steal_enabled"] = steal_enabled
                STATE["host_message"] = "Buzzer settings updated."

            elif action == "set_spyfall_settings":
                auto_start_vote = request.form.get("auto_start_vote_on_timer") == "on"
                allow_self_vote = request.form.get("allow_self_vote") == "on"
                STATE["spyfall_auto_start_vote_on_timer"] = auto_start_vote
                STATE["spyfall_allow_self_vote"] = allow_self_vote
                STATE["host_message"] = "Spyfall settings updated."

            elif action == "set_mafia_settings":
                seer_enabled = request.form.get("seer_enabled") == "on"
                auto_wolf_count = request.form.get("auto_wolf_count") == "on"
                reveal_roles_on_end = request.form.get("reveal_roles_on_end") == "on"
                wolf_count = STATE.get("mafia_wolf_count", 1)
                if not auto_wolf_count:
                    try:
                        wolf_count = int(request.form.get("wolf_count", wolf_count))
                    except (TypeError, ValueError):
                        wolf_count = STATE.get("mafia_wolf_count", 1)
                    wolf_count = max(1, min(2, wolf_count))
                STATE["mafia_seer_enabled"] = seer_enabled
                STATE["mafia_auto_wolf_count"] = auto_wolf_count
                STATE["mafia_wolf_count"] = wolf_count
                STATE["mafia_reveal_roles_on_end"] = reveal_roles_on_end
                STATE["host_message"] = "Mafia settings updated."
    
            elif action == "toggle_lobby_lock":
                STATE["lobby_locked"] = not STATE.get("lobby_locked", False)
                STATE["host_message"] = "Lobby locked." if STATE["lobby_locked"] else "Lobby unlocked."
    
            elif action == "toggle_allow_renames":
                STATE["allow_renames"] = not STATE.get("allow_renames", True)
                STATE["host_message"] = "Renames enabled." if STATE["allow_renames"] else "Renames disabled."
    
            elif action == "toggle_lobby_code":
                STATE["require_lobby_code"] = not STATE.get("require_lobby_code", True)
                STATE["host_message"] = (
                    "Lobby code required." if STATE["require_lobby_code"] else "Lobby code no longer required."
                )
    
            elif action == "set_timer_settings":
                timer_enabled = request.form.get("timer_enabled") == "on"
                auto_advance = request.form.get("auto_advance") == "on"
                try:
                    timer_seconds = int(request.form.get("timer_seconds", TIMER_DEFAULT_SECONDS))
                except ValueError:
                    timer_seconds = TIMER_DEFAULT_SECONDS
                try:
                    vote_timer_seconds = int(request.form.get("vote_timer_seconds", VOTE_TIMER_DEFAULT_SECONDS))
                except ValueError:
                    vote_timer_seconds = VOTE_TIMER_DEFAULT_SECONDS
                timer_seconds = max(10, min(180, timer_seconds))
                vote_timer_seconds = max(10, min(120, vote_timer_seconds))
                late_policy = request.form.get("late_submit_policy") or "lock_after_timer"
                if late_policy not in ("accept", "lock_after_timer"):
                    late_policy = "lock_after_timer"
                STATE["timer_enabled"] = timer_enabled
                STATE["timer_seconds"] = timer_seconds
                STATE["vote_timer_seconds"] = vote_timer_seconds
                STATE["auto_advance"] = auto_advance
                STATE["late_submit_policy"] = late_policy
                if not timer_enabled:
                    STATE["submissions_locked"] = False
                if timer_enabled and STATE.get("phase") == "in_round":
                    if STATE.get("mode") == "votebattle" and STATE.get("votebattle_phase") == "vote":
                        reset_timer_locked(STATE, vote_timer_seconds)
                    elif STATE.get("mode") == "spyfall" and STATE.get("spyfall_phase") == "vote":
                        reset_timer_locked(STATE, vote_timer_seconds)
                    else:
                        reset_timer_locked(STATE, timer_seconds)
                else:
                    STATE["timer_start_ts"] = None
                    STATE["timer_duration"] = None
                    STATE["timer_expired"] = False
                STATE["host_message"] = "Timer settings saved."
    
            elif action == "set_teams":
                teams_enabled = request.form.get("teams_enabled") == "on"
                try:
                    team_count = int(request.form.get("team_count", 2))
                except ValueError:
                    team_count = 2
                team_count = max(2, min(4, team_count))
                STATE["teams_enabled"] = teams_enabled
                STATE["team_count"] = team_count
                ensure_team_names(STATE)
                for team_id in range(1, team_count + 1):
                    name = (request.form.get(f"team_name_{team_id}") or "").strip()
                    if name:
                        STATE["team_names"][team_id] = name
                for pid in list(STATE.get("teams", {}).keys()):
                    if STATE["teams"].get(pid, 1) > team_count:
                        STATE["teams"].pop(pid, None)
                if teams_enabled:
                    for pid in STATE.get("players", {}):
                        if STATE.get("teams", {}).get(pid) not in range(1, team_count + 1):
                            assign_team_for_new_player(STATE, pid)
                STATE["host_message"] = "Teams updated."
    
            elif action == "randomize_teams":
                randomize_teams(STATE)
                STATE["host_message"] = "Teams randomized."
    
            elif action == "set_filter_mode":
                filter_mode = request.form.get("filter_mode", "mild")
                if filter_mode not in ("off", "mild", "strict"):
                    filter_mode = "mild"
                STATE["filter_mode"] = filter_mode
                requested_openai = request.form.get("openai_moderation_enabled") == "on"
                STATE["openai_moderation_enabled"] = bool(requested_openai and openai_ready())
                if requested_openai and not STATE["openai_moderation_enabled"]:
                    STATE["host_message"] = "OpenAI moderation not configured."
                else:
                    STATE["host_message"] = "Safety settings updated."
    
            elif action == "approve_reclaim":
                req_id = request.form.get("request_id")
                req = next((item for item in STATE.get("reclaim_requests", []) if item.get("request_id") == req_id), None)
                if not req:
                    STATE["host_message"] = "Reclaim request not found."
                else:
                    name = req.get("name", "")
                    new_pid = req.get("new_pid")
                    old_pid = find_pid_by_name(STATE, name)
                    STATE["reclaim_requests"] = [
                        item for item in STATE.get("reclaim_requests", []) if item.get("request_id") != req_id
                    ]
                    if new_pid:
                        if old_pid:
                            transfer_player_identity(STATE, old_pid, new_pid)
                        else:
                            existing_names = [info.get("name", "") for info in STATE.get("players", {}).values()]
                            unique_name = make_unique_name(name, existing_names)
                            STATE["players"][new_pid] = {"name": unique_name}
                            STATE["scores"][new_pid] = STATE.get("scores", {}).get(new_pid, 0)
                            assign_team_for_new_player(STATE, new_pid)
                        STATE.setdefault("reclaim_notices", {})[new_pid] = "Reclaim approved."
                    STATE["host_message"] = "Reclaim approved."
    
            elif action == "deny_reclaim":
                req_id = request.form.get("request_id")
                req = next((item for item in STATE.get("reclaim_requests", []) if item.get("request_id") == req_id), None)
                if not req:
                    STATE["host_message"] = "Reclaim request not found."
                else:
                    name = req.get("name", "")
                    new_pid = req.get("new_pid")
                    STATE["reclaim_requests"] = [
                        item for item in STATE.get("reclaim_requests", []) if item.get("request_id") != req_id
                    ]
                    if new_pid:
                        existing_names = [info.get("name", "") for info in STATE.get("players", {}).values()]
                        unique_name = make_unique_name(name, existing_names)
                        STATE["players"][new_pid] = {"name": unique_name}
                        STATE["scores"][new_pid] = 0
                        assign_team_for_new_player(STATE, new_pid)
                        STATE.setdefault("reclaim_notices", {})[new_pid] = f"Reclaim denied. Joined as {unique_name}."
                    STATE["host_message"] = "Reclaim denied."
    
            elif action == "apply_prompt_settings":
                prompt_mode = request.form.get("prompt_mode", "random")
                STATE["prompt_mode"] = "manual" if prompt_mode == "manual" else "random"
                STATE["manual_prompt_text"] = (request.form.get("manual_prompt_text") or "").strip()
                if "manual_wyr_a" in request.form:
                    STATE["manual_wyr_a"] = (request.form.get("manual_wyr_a") or "").strip()
                if "manual_wyr_b" in request.form:
                    STATE["manual_wyr_b"] = (request.form.get("manual_wyr_b") or "").strip()
                if "manual_trivia_0" in request.form:
                    STATE["manual_trivia_0"] = (request.form.get("manual_trivia_0") or "").strip()
                if "manual_trivia_1" in request.form:
                    STATE["manual_trivia_1"] = (request.form.get("manual_trivia_1") or "").strip()
                if "manual_trivia_2" in request.form:
                    STATE["manual_trivia_2"] = (request.form.get("manual_trivia_2") or "").strip()
                if "manual_trivia_3" in request.form:
                    STATE["manual_trivia_3"] = (request.form.get("manual_trivia_3") or "").strip()
                if "manual_correct_index" in request.form:
                    correct_raw = (request.form.get("manual_correct_index") or "").strip()
                    try:
                        STATE["manual_correct_index"] = int(correct_raw) if correct_raw else None
                    except ValueError:
                        STATE["manual_correct_index"] = None
                if "manual_wavelength_target_enabled" in request.form or "manual_wavelength_target" in request.form:
                    STATE["manual_wavelength_target_enabled"] = request.form.get("manual_wavelength_target_enabled") == "on"
                    target_raw = (request.form.get("manual_wavelength_target") or "").strip()
                    try:
                        STATE["manual_wavelength_target"] = int(target_raw) if target_raw else None
                    except ValueError:
                        STATE["manual_wavelength_target"] = None
                STATE["host_message"] = "Prompt settings saved."
    
            elif action == "pick_random_prompt":
                set_manual_prompt_from_random_locked(STATE["mode"])
                STATE["host_message"] = "Prompt filled from random."
    
            elif action == "votebattle_start_vote":
                if STATE["mode"] != "votebattle":
                    STATE["host_message"] = "Vote Battle voting is only for Vote Battle mode."
                elif STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round."
                elif STATE.get("votebattle_phase") != "submit":
                    STATE["host_message"] = "Voting already started."
                elif not STATE.get("votebattle_entries"):
                    STATE["host_message"] = "No entries submitted yet."
                else:
                    STATE["votebattle_phase"] = "vote"
                    STATE["submissions_locked"] = False
                    reset_timer_locked(STATE, STATE.get("vote_timer_seconds"))
                    STATE["host_message"] = "Vote Battle voting started."
    
            elif action == "spyfall_start_vote":
                if STATE["mode"] != "spyfall":
                    STATE["host_message"] = "Spyfall voting is only for Spyfall mode."
                elif STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round."
                elif STATE.get("spyfall_phase") != "question":
                    STATE["host_message"] = "Spy voting already started."
                else:
                    STATE["spyfall_phase"] = "vote"
                    STATE["submissions"] = {}
                    STATE["submissions_locked"] = False
                    reset_timer_locked(STATE, STATE.get("vote_timer_seconds"))
                    STATE["host_message"] = "Spyfall voting started."

            elif action == "buzzer_start_answer":
                if STATE["mode"] not in ("trivia_buzzer", "team_trivia"):
                    STATE["host_message"] = "Buzzer actions are only for Trivia Buzzer modes."
                elif STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round."
                elif STATE.get("trivia_buzzer_phase") != "buzz":
                    STATE["host_message"] = "Buzz phase is not active."
                elif not STATE.get("buzz_winner_pid"):
                    STATE["host_message"] = "No buzz yet."
                else:
                    STATE["trivia_buzzer_phase"] = "answer"
                    STATE["submissions_locked"] = False
                    reset_timer_locked(STATE, STATE.get("vote_timer_seconds"))
                    STATE["host_message"] = "Answer phase started."

            elif action == "buzzer_resolve_answer":
                if STATE["mode"] not in ("trivia_buzzer", "team_trivia"):
                    STATE["host_message"] = "Buzzer actions are only for Trivia Buzzer modes."
                elif STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round."
                elif STATE.get("trivia_buzzer_phase") != "answer":
                    STATE["host_message"] = "Answer phase is not active."
                elif STATE.get("answer_choice") is None:
                    STATE["host_message"] = "No answer yet."
                else:
                    correct_index = STATE.get("trivia_buzzer_correct_index")
                    if STATE.get("answer_choice") == correct_index:
                        compute_results_locked()
                        STATE["phase"] = "revealed"
                        STATE["submissions_locked"] = True
                        STATE["host_message"] = "Correct! Results revealed."
                    elif STATE.get("trivia_buzzer_steal_enabled", True):
                        STATE["trivia_buzzer_phase"] = "steal"
                        STATE["submissions_locked"] = False
                        reset_timer_locked(STATE, STATE.get("vote_timer_seconds"))
                        STATE["host_message"] = "Steal phase started."
                    else:
                        compute_results_locked()
                        STATE["phase"] = "revealed"
                        STATE["submissions_locked"] = True
                        STATE["host_message"] = "Incorrect. Results revealed."

            elif action == "mafia_start_day":
                if STATE["mode"] != "mafia":
                    STATE["host_message"] = "Mafia actions are only for Mafia mode."
                elif STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round."
                elif STATE.get("mafia_phase") != "night":
                    STATE["host_message"] = "Night is already resolved."
                else:
                    alive = list(STATE.get("mafia_alive", []))
                    victim = resolve_mafia_vote(STATE.get("mafia_wolf_votes", {}), alive)
                    if victim:
                        STATE["mafia_alive"] = [pid for pid in alive if pid != victim]
                        STATE["mafia_last_eliminated"] = victim
                    else:
                        STATE["mafia_last_eliminated"] = None
                    STATE["mafia_wolf_votes"] = {}
                    winner = check_mafia_win(STATE)
                    if winner:
                        STATE["mafia_phase"] = "over"
                        compute_results_locked()
                        STATE["phase"] = "revealed"
                        STATE["submissions_locked"] = True
                        STATE["host_message"] = f"{winner.title()} win!"
                    else:
                        STATE["mafia_phase"] = "day"
                        STATE["mafia_day_votes"] = {}
                        STATE["submissions_locked"] = False
                        reset_timer_locked(STATE, STATE.get("vote_timer_seconds"))
                        STATE["host_message"] = "Day started."
    
            elif action == "mafia_resolve_day":
                if STATE["mode"] != "mafia":
                    STATE["host_message"] = "Mafia actions are only for Mafia mode."
                elif STATE["phase"] != "in_round":
                    STATE["host_message"] = "No active round."
                elif STATE.get("mafia_phase") != "day":
                    STATE["host_message"] = "Day is not active."
                else:
                    alive = list(STATE.get("mafia_alive", []))
                    eliminated = resolve_mafia_vote(STATE.get("mafia_day_votes", {}), alive)
                    if eliminated:
                        STATE["mafia_alive"] = [pid for pid in alive if pid != eliminated]
                        STATE["mafia_last_eliminated"] = eliminated
                    else:
                        STATE["mafia_last_eliminated"] = None
                    winner = check_mafia_win(STATE)
                    if winner:
                        STATE["mafia_phase"] = "over"
                        compute_results_locked()
                        STATE["phase"] = "revealed"
                        STATE["submissions_locked"] = True
                        STATE["host_message"] = f"{winner.title()} win!"
                    else:
                        STATE["mafia_phase"] = "night"
                        STATE["mafia_wolf_votes"] = {}
                        STATE["mafia_day_votes"] = {}
                        STATE["mafia_seer_results"] = {}
                        STATE["submissions_locked"] = False
                        reset_timer_locked(STATE, STATE.get("timer_seconds"))
                        STATE["host_message"] = "Night started."
    
            elif action == "mafia_end_game":
                STATE["phase"] = "lobby"
                STATE["prompt"] = ""
                STATE["options"] = []
                STATE["correct_index"] = None
                STATE["submissions"] = {}
                STATE["submissions_locked"] = False
                STATE["mafia_phase"] = None
                STATE["mafia_roles"] = {}
                STATE["mafia_alive"] = []
                STATE["mafia_wolf_votes"] = {}
                STATE["mafia_day_votes"] = {}
                STATE["mafia_seer_results"] = {}
                STATE["mafia_last_eliminated"] = None
                stop_timer_locked(STATE)
                STATE["last_result"] = None
                STATE["host_message"] = "Mafia game ended."
    
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
    
            elif action == "award_quickdraw":
                pid = request.form.get("pid")
                if STATE["phase"] != "revealed":
                    STATE["host_message"] = "Points can only be awarded after reveal."
                elif STATE["mode"] != "quickdraw":
                    STATE["host_message"] = "Award points is only for Quick Draw."
                elif STATE.get("quickdraw_scoring") != "host":
                    STATE["host_message"] = "Quick Draw is not in host-pick scoring."
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
        submission_target = get_submission_target_count(snapshot)
        progress_percent = int((get_active_submission_count(snapshot) / submission_target) * 100) if submission_target else 0
        show_progress_button, progress_label = get_progress_ui(
            snapshot.get("mode", ""),
            snapshot.get("phase", ""),
            snapshot.get("votebattle_phase"),
            snapshot.get("spyfall_phase"),
            snapshot.get("mafia_phase"),
            snapshot.get("trivia_buzzer_phase"),
        )
        buzz_winner_pid = snapshot.get("buzz_winner_pid")
        buzz_winner_name = snapshot.get("players", {}).get(buzz_winner_pid, {}).get("name") if buzz_winner_pid else ""
        buzz_team_id = snapshot.get("buzz_winner_team_id")
        buzz_team_label = snapshot.get("team_names", {}).get(buzz_team_id) if buzz_team_id else ""
        buzz_winner_display = (
            f"{buzz_winner_name} ({buzz_team_label})"
            if buzz_winner_name and buzz_team_label
            else buzz_winner_name
            if buzz_winner_name
            else "--"
        )
        answer_pid = snapshot.get("answer_pid")
        answer_name = snapshot.get("players", {}).get(answer_pid, {}).get("name") if answer_pid else ""
        answer_team_id = snapshot.get("answer_team_id")
        answer_team_label = snapshot.get("team_names", {}).get(answer_team_id) if answer_team_id else ""
        answer_display = (
            f"{answer_name} ({answer_team_label})"
            if answer_name and answer_team_label
            else answer_name
            if answer_name
            else "--"
        )
        return jsonify(
            {
                "player_count": len(snapshot.get("players", {})),
                "submission_count": get_active_submission_count(snapshot),
                "submission_target": submission_target,
                "progress_percent": progress_percent,
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
                "votebattle_phase": snapshot.get("votebattle_phase"),
                "votebattle_submit_count": len(snapshot.get("votebattle_entries", {})),
                "votebattle_vote_count": len(snapshot.get("votebattle_votes", {})),
                "spyfall_phase": snapshot.get("spyfall_phase"),
                "mafia_phase": snapshot.get("mafia_phase"),
                "trivia_buzzer_phase": snapshot.get("trivia_buzzer_phase"),
                "submissions_locked": snapshot.get("submissions_locked", False),
                "timer_remaining": get_timer_remaining(snapshot),
                "show_progress_button": show_progress_button,
                "progress_label": progress_label,
                "buzz_winner_display": buzz_winner_display,
                "answer_display": answer_display,
            }
        )
    
    
    @app.get("/api/public_state")
    def api_public_state() -> Any:
        snapshot = get_state_snapshot()
        return jsonify(
            {
                "phase": snapshot.get("phase"),
                "mode": snapshot.get("mode"),
                "round_id": snapshot.get("round_id", 0),
                "votebattle_phase": snapshot.get("votebattle_phase"),
                "spyfall_phase": snapshot.get("spyfall_phase"),
                "mafia_phase": snapshot.get("mafia_phase"),
                "trivia_buzzer_phase": snapshot.get("trivia_buzzer_phase"),
                "submissions_locked": snapshot.get("submissions_locked", False),
                "timer_remaining": get_timer_remaining(snapshot),
            }
        )
    
    
    @app.get("/api/host_timer")
    def api_host_timer() -> Any:
        if not is_host_request():
            return jsonify({"error": "host required"}), 403
        with STATE_LOCK:
            remaining = tick_timer_locked(STATE)
            locked = STATE.get("submissions_locked", False)
        return jsonify({"timer_remaining": remaining, "submissions_locked": locked})


if FLASK_AVAILABLE:
    app = Flask(__name__)
    register_routes(app)
else:
    app = None


def print_startup_info(port: int, lobby_code: str) -> Tuple[str, str]:
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
    print(f"Lobby code: {lobby_code}")
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


def run_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


class PartyHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._state_backup = copy.deepcopy(STATE)

    def tearDown(self) -> None:
        STATE.clear()
        STATE.update(copy.deepcopy(self._state_backup))

    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("  The   Quick  "), "quick")
        self.assertEqual(normalize_text("An apple"), "apple")

    def test_profanity_filter(self) -> None:
        self.assertTrue(contains_banned_word("This is damn", "mild"))
        self.assertFalse(contains_banned_word("This is darn", "off"))

    def test_unique_answer_scoring(self) -> None:
        submissions = {"p1": "Apple", "p2": "apple", "p3": "Banana"}
        unique = unique_answer_pids(submissions)
        self.assertEqual(set(unique), {"p3"})

    def test_lobby_code_validation(self) -> None:
        self.assertTrue(validate_lobby_code("ab cd", "ABCD", True))
        self.assertFalse(validate_lobby_code("", "ABCD", True))
        self.assertTrue(validate_lobby_code("", "ABCD", False))

    def test_spy_selection_in_players(self) -> None:
        with STATE_LOCK:
            STATE["players"] = {"a": {"name": "A"}, "b": {"name": "B"}, "c": {"name": "C"}}
            assign_spyfall_roles(STATE, ["Role1", "Role2"])
            self.assertIn(STATE.get("spyfall_spy_pid"), STATE["players"])

    def test_vote_tally_winners(self) -> None:
        winners, max_votes = pick_winners_from_tally({"p1": 2, "p2": 2, "p3": 1})
        self.assertEqual(set(winners), {"p1", "p2"})
        self.assertEqual(max_votes, 2)

    def test_select_buzz_winner(self) -> None:
        winner_pid, winner_ts = select_buzz_winner(None, None, "p1", 10.0)
        self.assertEqual(winner_pid, "p1")
        self.assertEqual(winner_ts, 10.0)
        winner_pid, winner_ts = select_buzz_winner("p1", 10.0, "p2", 5.0)
        self.assertEqual(winner_pid, "p2")
        self.assertEqual(winner_ts, 5.0)
        winner_pid, winner_ts = select_buzz_winner("p1", 10.0, "p2", 12.0)
        self.assertEqual(winner_pid, "p1")
        self.assertEqual(winner_ts, 10.0)

    def test_trivia_buzzer_scoring(self) -> None:
        outcome = compute_trivia_buzzer_outcome(2, "p1", "p1", 2, {})
        self.assertEqual(outcome.get("points"), 2)
        self.assertEqual(outcome.get("scoring_pid"), "p1")
        outcome = compute_trivia_buzzer_outcome(2, "p1", "p1", 1, {"p2": 2})
        self.assertEqual(outcome.get("points"), 1)
        self.assertEqual(outcome.get("scoring_pid"), "p2")

    def test_draw_from_pool_no_repeat_until_exhausted(self) -> None:
        state: Dict[str, Any] = {}
        draws = [draw_from_pool(state, "test", 3) for _ in range(3)]
        self.assertEqual(len(set(draws)), 3)

    def test_draw_from_pool_avoids_immediate_repeat_after_refill(self) -> None:
        state: Dict[str, Any] = {"prompt_bags": {"test": [0]}, "prompt_last": {"test": 0}}
        rng_state = random.getstate()
        random.seed(123)
        try:
            first = draw_from_pool(state, "test", 2)
            second = draw_from_pool(state, "test", 2)
        finally:
            random.setstate(rng_state)
        self.assertEqual(first, 0)
        self.assertNotEqual(second, 0)

    def test_pool_key_for_mode_trivia_shared(self) -> None:
        self.assertEqual(pool_key_for_mode("trivia_buzzer"), "trivia")
        self.assertEqual(pool_key_for_mode("team_trivia"), "trivia")

    def test_progress_ui_labels(self) -> None:
        show, label = get_progress_ui("votebattle", "in_round", votebattle_phase="submit")
        self.assertTrue(show)
        self.assertEqual(label, "Start Vote Battle Voting")
        show, label = get_progress_ui("votebattle", "in_round", votebattle_phase="vote")
        self.assertTrue(show)
        self.assertEqual(label, "Reveal Results")
        show, label = get_progress_ui("spyfall", "in_round", spyfall_phase="question")
        self.assertTrue(show)
        self.assertEqual(label, "Start Spy Vote")
        show, label = get_progress_ui("spyfall", "in_round", spyfall_phase="vote")
        self.assertTrue(show)
        self.assertEqual(label, "Reveal Results")
        show, label = get_progress_ui("mafia", "in_round", mafia_phase="night")
        self.assertTrue(show)
        self.assertEqual(label, "Resolve Night / Start Day")
        show, label = get_progress_ui("mafia", "in_round", mafia_phase="day")
        self.assertTrue(show)
        self.assertEqual(label, "Resolve Day Vote")
        show, label = get_progress_ui("mafia", "revealed", mafia_phase="over")
        self.assertTrue(show)
        self.assertEqual(label, "End Mafia Game")
        show, label = get_progress_ui("trivia_buzzer", "in_round", trivia_buzzer_phase="buzz")
        self.assertTrue(show)
        self.assertEqual(label, "Start Answer")
        show, label = get_progress_ui("trivia_buzzer", "in_round", trivia_buzzer_phase="answer")
        self.assertTrue(show)
        self.assertEqual(label, "Resolve Answer")
        show, label = get_progress_ui("trivia_buzzer", "in_round", trivia_buzzer_phase="steal")
        self.assertTrue(show)
        self.assertEqual(label, "Reveal Results")
        show, label = get_progress_ui("team_trivia", "in_round", trivia_buzzer_phase="buzz")
        self.assertTrue(show)
        self.assertEqual(label, "Start Answer")
        show, label = get_progress_ui("team_trivia", "in_round", trivia_buzzer_phase="answer")
        self.assertTrue(show)
        self.assertEqual(label, "Resolve Answer")
        show, label = get_progress_ui("team_trivia", "in_round", trivia_buzzer_phase="steal")
        self.assertTrue(show)
        self.assertEqual(label, "Reveal Results")

    @unittest.skipUnless(FLASK_AVAILABLE, "Flask not installed")
    def test_flask_join_and_host_lock(self) -> None:
        with STATE_LOCK:
            STATE["players"] = {}
            STATE["scores"] = {}
            STATE["lobby_locked"] = False
            STATE["require_lobby_code"] = False
            STATE["lobby_code"] = "ABCDE"
        client = app.test_client()
        resp = client.post("/join", data={"name": "Alice", "lobby_code": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("pid=", resp.headers.get("Set-Cookie", ""))

        resp_remote = client.get(f"/host?key={HOST_KEY}", environ_base={"REMOTE_ADDR": "1.2.3.4"})
        self.assertNotIn("host=", resp_remote.headers.get("Set-Cookie", ""))

        resp_local = client.get(f"/host?key={HOST_KEY}", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertIn("host=", resp_local.headers.get("Set-Cookie", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Party Hub server")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument("--test", action="store_true", help="Run tests and exit")
    args = parser.parse_args()

    if args.test:
        raise SystemExit(run_tests())

    if not FLASK_AVAILABLE:
        print("Flask is not installed. Activate your venv and run: pip install flask waitress")
        raise SystemExit(1)

    with STATE_LOCK:
        STATE["lobby_code"] = make_lobby_code(JOIN_CODE_LENGTH)
        lobby_code = STATE["lobby_code"]

    join_url, host_url = print_startup_info(args.port, lobby_code)
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
