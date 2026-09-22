# Riot Games Compliance Position

[🇧🇷 Português](COMPLIANCE.md) · 🇺🇸 English

This document exists because *"will this get me banned?"* is the first question every prospective
user asks, and because a vague answer is worth nothing. Here is the specific one.

## The rule we are staying on the right side of

Riot prohibits third-party software that automates gameplay or surfaces information a player could
not otherwise obtain during a game. They publish and permit the Riot Web API, the Live Client Data
API and the Replay API for legitimate third-party tools.

RiftCoach does not automate anything and does not run during live games at all.

## What RiftCoach touches

| Surface | When | Why it is fine |
|---|---|---|
| `match-v5/matches/*` and `/timeline` | After a game is over | The official public API, on completed matches, with the user's own key. |
| DataDragon / CommunityDragon | Any time | Public static asset CDNs. |
| `127.0.0.1:2999/replay/*` | **Only during replay playback** | Documented Replay API; the routes do not exist outside replay mode. |
| `127.0.0.1:2999/liveclientdata/*` | **Only during replay playback**, gated behind the same check | Reading a replay's state, not a live game's. |
| `.rofl` files | On disk, after a game | Metadata only. |

## What RiftCoach never does

- Run, infer, alert or display anything during a live game
- Overlay anything onto a live game
- Simulate input, automate actions, or interact with the game process
- Read game memory or inject code
- Reveal information not available to the player (no fog-of-war data, no enemy cooldowns from
  hidden state, no jungle tracking during live play)
- Scrape third-party sites (op.gg, u.gg, porofessor) or violate their terms
- Transmit user data anywhere the user did not explicitly configure

## The enforcement mechanism

This is not a policy statement — it is enforced in code, in one place.

`riftcoach/replay/guard.py` is the **only** module permitted to open a connection to
`127.0.0.1:2999`. Before every request it issues:

```
GET https://127.0.0.1:2999/replay/playback
```

That route exists **only** while a replay is playing. During a live game it returns 404 while
`/liveclientdata/*` continues to answer — so a 404 here is a positive signal that a live game may be
in progress, and we refuse unconditionally.

Three properties make this trustworthy:

1. **It fails closed.** Connection error, 404, timeout, unexpected body — all raise
   `LiveGameRefused`. There is no code path where an ambiguous result proceeds.
2. **It re-checks before every request, not once per session.** A user can alt-tab out of a replay
   into champion select mid-analysis. A five-minute batch job must notice.
3. **It is enforced by architecture, not discipline.** The check is in the transport layer. A
   contributor cannot accidentally bypass it by writing a new feature, because there is no other
   client.

An import-linter rule in CI fails the build if any module outside `riftcoach/replay/` imports
`httpx` and references port 2999.

## TLS

The Live Client Data API uses Riot's self-signed certificate. We ship Riot's published
`riotgames.pem` and pin against it rather than using `verify=False`. It costs nothing and it means a
local MITM cannot feed the app fabricated game state.

## API keys and data handling

- Users supply their own Riot API key. There is no RiftCoach-operated server, proxy or telemetry.
- Keys are stored in the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service),
  never in a file in the repository.
- Match data is cached locally on the user's machine and is never transmitted anywhere except to an
  LLM provider the user explicitly configured. `privacy_mode: strict` disables that entirely and is
  enforced at the router level, before provider selection.
- The benchmark dataset published by the project contains only aggregate percentiles. No PUUIDs, no
  per-player rows, no identifiable data.

## The replay overlay

RiftCoach draws marks on top of the game window **while a replay is playing**. Since that is the
most direct question anyone can ask about compliance, here is the whole answer.

**The distinction is between a live game and a recording, and it is structural, not rhetorical.**

| | Live game | Replay |
|---|---|---|
| Is the game over? | No | Yes, and the result is already in your history |
| Competitive advantage? | It would be | There is no game in progress to win |
| What does Riot provide for it? | Nothing | The documented Replay API, with camera and time control |
| Can RiftCoach get there? | **No** — `ReplayGuard` refuses | Yes |

The overlay is an ordinary Windows process drawing in its own transparent window. It **does not
touch the game process**: no code injection, no memory reads, no drawing inside the renderer, no
input. It reads the replay clock through the official Replay API — the same one already used for
the jump-to-moment button — and draws alongside.

**The interlock is unchanged, and it is still the only path.** The overlay opens no socket, knows
no port and builds no URL: it asks `ReplayController`, which asks `ReplayGuard`, which checks
`GET /replay/playback` **before every request**. In a live game that route 404s and the overlay
never comes into existence. Nothing was loosened to let this feature in.

On the marking hotkeys (`Ctrl+Alt+E` and friends): RiftCoach **reads** keyboard state from the OS,
only while the League window is in the foreground, and **never sends** a key or click anywhere. It
is the same thing a screen recorder with a global hotkey does.

## Contributor policy

PRs that introduce live-game functionality are closed without review. This includes live overlays,
live alerts, "read-only" live HUDs, and anything that reads client state outside replay mode — no
matter how it is framed. The constraint is what makes the project safe to recommend, and it is not
negotiable for any feature.

The replay overlay is not an exception to that rule: it goes through the same `ReplayGuard`, and
that is precisely why it could exist. A PR that draws on screen without going through it is a
live-game PR, even if today it only runs in replay.

## Disclaimer

RiftCoach AI is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games
or anyone officially involved in producing or managing Riot Games properties. League of Legends and
Riot Games are trademarks or registered trademarks of Riot Games, Inc.

If Riot Games requests a change to this project, open an issue and we will comply.
