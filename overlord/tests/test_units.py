import threading
import time
from datetime import datetime, timezone

from overlord import analyze, scoring, seo
from overlord.profiles import DEFAULT_PROFILE, create_client, get_client, rollback_profile, update_profile
from overlord.publish.scheduler import next_slots


def _words(text_by_sentence, gap=0.6):
    out, t = [], 0.0
    for s in text_by_sentence:
        for w in s.split():
            out.append({"w": w, "s": round(t, 2), "e": round(t + 0.3, 2), "p": 0.9})
            t += 0.35
        t += gap
    return out


SENTS = ["Nobody tells you this secret about money.", "I lost two lakh rupees in one week.",
         "Here is the mistake I made.", "So what should you do instead?", "Write every deal down on paper.",
         "Morning routines are overrated.", "A fixed start time works better.", "I tested this for ninety days.",
         "My output doubled after that.", "And that is why it works."]


def test_candidates_are_sentence_aligned_and_ranked():
    words = _words(SENTS)
    sents = analyze.sentences(words)
    assert len(sents) == len(SENTS)
    cands = scoring.generate(sents, 4, 12)
    assert cands and all(4 <= c.dur <= 12 + 0.31 for c in cands)
    word_starts = {round(w["s"] - 0.15, 2) for w in words} | {0.0}
    assert all(round(c.s, 2) in word_starts for c in cands)  # every candidate starts on a word boundary
    trimmed = [c for c in cands if "trimmed_lead" in c.reasons]
    assert trimmed and all(c.text.split()[0].lower() not in ("so", "and") for c in trimmed)
    for c in cands:
        scoring.score(c, sents, 0.5, DEFAULT_PROFILE)
        assert set(scoring.DIMENSIONS) <= set(c.scores) and all(0 <= v <= 1 for v in c.scores.values())
        assert all(d in c.reasons for d in scoring.DIMENSIONS)  # every score is explained


def test_dangling_start_penalised():
    sents = analyze.sentences(_words(SENTS))
    good = scoring.Candidate(0, 1, sents[0].s, sents[1].e, sents[0].text + " " + sents[1].text)
    bad = scoring.Candidate(9, 9, sents[9].s, sents[9].e, sents[9].text)  # starts with "And"
    for c in (good, bad):
        scoring.score(c, sents, 0.5, DEFAULT_PROFILE)
    assert bad.scores["standalone"] < good.scores["standalone"]


def test_selection_avoids_overlap_duplicates_and_reports_shortfall():
    sents = analyze.sentences(_words(SENTS))
    cands = scoring.generate(sents, 3, 10)
    for c in cands:
        scoring.score(c, sents, 0.5, DEFAULT_PROFILE)
    chosen, rep = scoring.rank_and_select(cands, 50, 0.0)
    for i, a in enumerate(chosen):
        for b in chosen[i + 1:]:
            assert scoring._overlap(a, b) <= 0.25
    assert rep["shortfall"] > 0 and "not manufactured" in rep["shortfall_reason"]
    none, rep2 = scoring.rank_and_select(cands, 3, 0.99)
    assert none == [] and rep2["rejected"]["below_threshold"] == len(cands)


def test_scheduler_respects_cap_windows_and_taken_slots():
    pol = {"timezone": "Asia/Kolkata", "windows": ["12:30", "18:30", "21:00"], "max_per_day": 2}
    now = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
    s = next_slots(5, pol, [], now)
    assert s[:2] == ["2026-11-25T07:00:00Z", "2026-11-25T13:00:00Z"]  # 12:30 and 18:30 IST
    days = [x[:10] for x in s]
    assert all(days.count(d) <= 2 for d in days)
    s2 = next_slots(2, pol, ["2026-11-25T07:00:00Z"], now)
    assert "2026-11-25T07:00:00Z" not in s2 and [x[:10] for x in s2].count("2026-11-25") == 1


def test_profiles_isolated_versioned_and_rollbackable(env):
    st, db = env
    a, b = create_client(db, "A"), create_client(db, "B")
    update_profile(db, a, {"captions": {"highlight": "#FF0000"}}, note="client A prefers red")
    assert get_client(db, a)["profile"]["captions"]["highlight"] == "#FF0000"
    assert get_client(db, b)["profile"]["captions"]["highlight"] == DEFAULT_PROFILE["captions"]["highlight"]
    assert get_client(db, a)["profile"]["captions"]["font"] == DEFAULT_PROFILE["captions"]["font"]  # deep merge
    rollback_profile(db, a, 1)
    assert get_client(db, a)["profile"]["captions"]["highlight"] == DEFAULT_PROFILE["captions"]["highlight"]
    assert get_client(db, a)["profile_version"] == 3
    assert get_client(db, a)["publish"]["autonomous"] is False  # approval mode by default


def test_duplicate_jobs_and_lease_recovery(env):
    st, db = env
    cid = create_client(db, "C")
    j1, new1 = db.create_job(cid, "x.mp4", "file:abc", "own_content", 5, 15, 59)
    j2, new2 = db.create_job(cid, "x.mp4", "file:abc", "own_content", 5, 15, 59)
    assert new1 and not new2 and j1 == j2
    claims = []
    ts = [threading.Thread(target=lambda: claims.append(db.claim_job(f"w{i}", lease_s=0.5))) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(1 for c in claims if c) == 1  # exactly one worker gets the job
    assert db.claim_job("other", 60) is None  # lease still valid
    time.sleep(0.6)  # worker "crashed": lease expires
    again = db.claim_job("other", 60)
    assert again and again["id"] == j1 and again["attempts"] == 2


def test_seo_validation_limits_and_avoid_words():
    m = seo.validate({"title": "x" * 150, "description": "d", "tags": ["a" * 70] * 20 + ["<bad>"], "hashtags": ["shorts", "#Money!"]})
    assert len(m["title"]) <= 100 and m["hashtags"] == ["#shorts", "#Money"]
    assert sum(len(t) + 1 for t in m["tags"]) <= 500 and all("<" not in t for t in m["tags"])
    fb = seo.fallback("I lost two lakh rupees. Here is the mistake I made.", ["Lakh"], ["#shorts"])
    assert fb["source"] == "extractive" and "lakh" in fb["title"].lower()
    try:
        seo.validate({"title": "Guaranteed viral", "description": "", "tags": [], "hashtags": []}, ["guaranteed"])
        raise AssertionError("avoid word not enforced")
    except ValueError:
        pass


def test_third_party_content_needs_permission_reference(env):
    from overlord.publish.publisher import eligible

    st, db = env
    cid = create_client(db, "Clipper")
    pol = get_client(db, cid)["publish"] | {"autonomous": True}
    j1, _ = db.create_job(cid, "u", "yt:a", "clipping_program", 3, 15, 59)
    j2, _ = db.create_job(cid, "u", "yt:b", "clipping_program", 3, 15, 59, rights_ref="https://whop.com/campaign/x")
    j3, _ = db.create_job(cid, "u", "yt:c", "own_content", 3, 15, 59)
    clip = {"qc_pass": 1, "path": __file__, "approval": "pending"}
    ok1, why1 = eligible(db, {**clip, "job_id": j1}, pol)
    assert not ok1 and "permission" in why1
    assert eligible(db, {**clip, "job_id": j2}, pol)[0]
    assert eligible(db, {**clip, "job_id": j3}, pol)[0]
