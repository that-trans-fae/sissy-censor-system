# Handoff: video-level sex attribution

Written Oct 18, 2026. For an agent picking this up in a fresh worktree.
Read `CLAUDE.md` first; its conventions (config over constants, sample
review, never view samples, commits only when asked) all apply.

## The problem

Video jobs blur male subjects whose chests the model reads as female.
Reported case: Pornhub viewkey `6710b29dca308`, a 437 s vertical video
(720x1278 source, 406x720 after scaling, 45 fps capped to 30) that the user
says should never be censored. The proxy censored 8,496 frames.

Reproduced by running the real `censor_video()` path on the stream with
instrumentation (scores only; nothing viewed or kept):

- Every censored box is a genuine detection, not a fail-closed cover:
  1,188 `FEMALE_BREAST_EXPOSED` censors across 812 of 1,640 samples
  (584 at the plain threshold, 597 lowered by "corroborated by
  FEMALE_BREAST_EXPOSED", 7 "male context only"), plus 12 weak
  `FEMALE_GENITALIA_EXPOSED`.
- The model is confidently wrong: breast scores median 0.44, p90 0.62,
  max 0.76. The per-box male breast score stays under 0.26 (the
  `male_score_ratio` idea in `decisions.md` can't help).
- 640m (run on every 4th sample) agrees on the breasts (median 0.43,
  196 of 351 boxes at 0.4+).

What the rest of the video shows (counts of detections at 0.25+):

| | 320n (1,640 samples) | 640m (410 samples) |
| --- | --- | --- |
| `MALE_GENITALIA_EXPOSED` | 164 | 228 |
| `FACE_MALE` | 55 | 73 |
| `FACE_FEMALE` | 162 | 3 |
| `FEMALE_GENITALIA_EXPOSED` | 23 | 8 |
| `MALE_BREAST_EXPOSED` | 124 | 22 |

Why per-frame rules can't fix it:

- A penis or male face is in the same frame as a breast box in only 60 of
  1,123 samples under 320n (75 of 215 under 640m).
- 320n reads the face as female more often than male, and a pair of breast
  boxes counts as female context for each other, so `decide()`'s "male
  context only" rule almost never applies.
- "Penis in frame → pass breasts" would leak real breasts: in the browsing
  log, 200 of 1,244 images with a censored breast also have a penis at
  0.35+ (straight porn).
- The penis-priority rule (`yields_to_male`, committed in `2bdeded`) only
  covers a breast box that overlaps a penis; it barely touches this video.

## What to build

Attribution over time, not per frame: evidence across the video says
"this subject is male" even when a given frame doesn't. Suggested starting
point; validate before committing to it:

- Keep a rolling window (on the order of 30 s of samples) of sex evidence
  per job: confident `MALE_GENITALIA_EXPOSED`, `FACE_MALE`,
  `FEMALE_GENITALIA_EXPOSED`, maybe `FACE_FEMALE` (unreliable under 320n,
  see above).
- When the window is male-dominated (lots of male evidence, almost no
  female genitalia), treat breast boxes as male: raise their threshold
  (e.g. to `male_context_threshold`) or pass them, and stop pairs
  corroborating each other.
- Consider running 640m sparsely (e.g. 1 fps) for attribution only: it
  sees male faces far more reliably here. 320n can keep doing the boxes.
- The start of a video has no history. Options: a short lookahead (the
  job already buffers unsampled frames until the next sample, and
  `video.ready_seconds` delays playback anyway), or a quick pre-scan
  sampling the whole video before starting. Decide with the user; mind
  that jobs stream fMP4 while running, so frames already written can't
  be un-blurred.
- Everything behind config keys with documented defaults (`defaults.yaml`,
  the dataclass, `_build()`), plus a row in `docs/decisions.md`. Video
  code: `src/censor_proxy/video.py` (`_pump()` is the per-frame loop);
  tests in `src/tests/test_video.py`.

The hard part is not regressing straight content: a woman's real breasts
with a man in frame must stay censored. Get counter-examples before tuning.

## Data and scripts

All under `/home/marissa/Repositories/scs/docs/samples/video/` in the main
checkout (gitignored, so not in your worktree; use the absolute path):

- `6710b29dca308.scores.json`: every 320n sample of the reported video
  (one per 8 frames, 1,640 total). Each entry has `i`, `shape`, and `d`,
  the detections with label, score, box and `s` (all class scores at
  0.05+); every 4th entry also has `d640`, the 640m detections. Replay
  candidate rules against this offline: no network needed.
- `dump_scores.py URL OUT.json`: produces that file for another video by
  running the real job path. `instrument_job.py URL`: the same, printing
  censor counts by label and reason, and fail-closed vs region covers.
  Both expect to run from `src/` (copy them in, delete afterwards) inside
  the image, with the HLS URL from the proxy log (the injected script logs
  `"event":"start"` with `"found":"<m3u8 url>"` when `video.debug` is on,
  or ask the user for it). Pass the source's Referer; the CDN answers 410
  otherwise.

Fetching any new video, even scores-only, needs the user's go-ahead first.
They gave it for this one. Ask them for a few straight videos with women
(must stay censored) and other male-subject videos (must pass), and never
view the frames.

## Worktree practicalities

- **Samples:** `docs/samples/` isn't in the worktree, so
  `tests/test_samples.py` skips. Mount the main checkout's samples into the
  test container:
  `-v /home/marissa/Repositories/scs/docs/samples:/w/docs/samples:ro`
  (Docker Desktop only mounts paths under `/home`; the worktree must be
  under `/home` too).
- **The user's live proxy:** don't `docker compose up` from the worktree.
  The compose project name comes from the directory name (`src`), so it
  would replace the user's running proxy with your branch. Test with
  `docker run` as in `CLAUDE.md`, and ask before touching the live
  container.
- **Other sessions:** other sessions work on `main` at the same time;
  `config.py` and `defaults.yaml` see frequent edits (a whitelist refactor
  was uncommitted when this was written). Keep your changes to them small
  and expect to merge.
- **Committing:** `CLAUDE.md` says commits go on `main` when asked; in a
  worktree, ask the user how they want the branch brought back.
