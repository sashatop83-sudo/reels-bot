#!/usr/bin/env python3
"""Быстрые проверки ядра бота без Telegram/Groq."""

import time

from bot.db import init_db, try_start_welcome, claim_update, can_process_video, remaining_videos
from bot.services.transcribe import (
    SubtitleSegment,
    Word,
    align_segments_to_duration,
    rebuild_segments_from_lines,
    update_segment_words,
)
from bot.services.subtitles import build_ass, _all_words


def test_double_start_guard():
    init_db()
    uid = 900_000 + int(time.time()) % 50_000
    assert try_start_welcome(uid, cooldown=60) is True
    assert try_start_welcome(uid, cooldown=60) is False
    print("start guard OK")


def test_update_dedup():
    uid = 100_000 + int(time.time()) % 50_000
    assert claim_update(uid) is True
    assert claim_update(uid) is False
    print("update dedup OK")


def test_align_no_early_cut():
    """Речь 80с, metadata контейнера 60с — не сжимать, иначе пропадёт хвост."""
    segs = [
        SubtitleSegment(0, 40, "a", [Word("a", 0, 40)]),
        SubtitleSegment(40, 80, "b", [Word("b", 40, 80)]),
    ]
    out = align_segments_to_duration(segs, duration=60, audio_duration=80)
    assert out[-1].end == 80, f"expected 80 got {out[-1].end}"
    print("align no early cut OK")


def test_align_compress_when_needed():
    segs = [SubtitleSegment(0, 100, "x", [Word("x", 0, 100)])]
    out = align_segments_to_duration(segs, duration=50, audio_duration=50)
    assert abs(out[-1].end - 50) < 0.01
    print("align compress OK")


def test_text_edit_keeps_timing():
    old = [
        SubtitleSegment(0, 2, "привет", [Word("привет", 0, 2)]),
        SubtitleSegment(2, 5, "мир", [Word("мир", 2, 5)]),
    ]
    new = rebuild_segments_from_lines(old, ["привет", "мир тут"])
    assert new[-1].words[-1].end >= 4.5
    print("edit timing OK")


def test_admin_unlimited():
    init_db()
    admin_id = 42_000_000 + int(time.time()) % 100_000
    assert can_process_video(admin_id, free_limit=2, admin_user_id=admin_id) is True
    assert remaining_videos(admin_id, free_limit=2, admin_user_id=admin_id) == "безлимит"
    other_id = admin_id + 1
    assert can_process_video(other_id, free_limit=0, admin_user_id=admin_id) is False
    print("admin unlimited OK")


def test_ass_build():
    segs = [SubtitleSegment(0, 3, "тест субтитров", [Word("тест", 0, 1), Word("субтитров", 1, 3)])]
    assert _all_words(segs)
    ass = build_ass(segs, "hormozi", "montserrat", "center", "default", 1080, 1920)
    assert "Dialogue:" in ass
    print("ass build OK")


if __name__ == "__main__":
    test_double_start_guard()
    test_update_dedup()
    test_align_no_early_cut()
    test_align_compress_when_needed()
    test_text_edit_keeps_timing()
    test_admin_unlimited()
    test_ass_build()
    print("\nALL OK")
