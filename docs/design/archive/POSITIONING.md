# BlackBoxRS: Positioning

## 1. One-sentence definition

> BlackBoxRS turns ROS 2 robot failures into reproducible incident
> bundles and preflight rules that stop the same failure from
> happening twice.

## 2. Tagline options

1. **"Your robot failed. Now what?"**: postmortem-first framing;
   plays to engineers who have lost an afternoon to triage.
2. **"Incident intelligence for ROS 2."**: straight category claim;
   read by platform/SRE-minded buyers as "we are not a logger."
3. **"Capture the failure, prevent the recurrence."**: emphasises
   the loop, the part competitors don't have.

Default to (1) on marketing surfaces, (2) in technical README, (3)
when speaking to a fleet operator.

## 3. Old framing vs new framing

| Old (drop) | New (use) |
|---|---|
| Flight recorder for ROS 2 | Incident intelligence for ROS 2 |
| Observability daemon | Reliability infrastructure |
| JSONL telemetry stream | Reproducible incident bundles |
| Anomaly detection | Failure capture + prevention loop |
| Single-host monitoring | Local-first; built for the robot, not the cloud |
| Logs and metrics | Evidence and answers |

If a sentence works equally well for Datadog, it does not work for
BlackBoxRS. Cut it.

## 4. Ideal customer profiles

### ICP 1: Robotics platform / reliability engineer

- 5–50 robots in the field. Pixel-counted release process.
- They own the launch readiness review and the postmortem.
- Their day involves Slack pings about "robot 14 is acting weird."
- Procurement: usually sneaks BlackBoxRS into the standard image
  before formally buying.
- Pricing posture: per-fleet or per-host self-hosted; "cloud SaaS
  observability" pricing is poison here.

### ICP 2: Field robotics startup (Series Seed–B)

- 1–20 engineers. ROS 2 Humble or Iron.
- One person does "DevOps + reliability" in their copious spare
  time.
- Active customers exist but the robot is still misbehaving in
  ways the team can't reproduce.
- Procurement: founder buys it on a credit card.
- Pricing posture: open source first; pay only when there is a
  feature they genuinely need (multi-host aggregation, prevention
  library sharing).

### ICP 3: Robotics research lab

- ROS 2 on GO2 / Spot / TurtleBot / custom platforms.
- Grad students burning hours on "yesterday it worked."
- Procurement: zero. They use the OSS.
- Why we still care: they generate the bug reports and code samples
  that grow the prevention library and the feature set.

Not ICPs (yet):

- ROS 1 sites.
- Pure simulation (Gazebo / Isaac Sim / Mujoco) shops with no
  hardware. The wedge is hardware-coupled failure.
- Cloud-scale dashboarding buyers. We are not Grafana for robots.

## 5. Top customer pains

1. **"It worked yesterday."** Same launch, same robot, different
   behaviour. Today there is no one-command answer to "what
   changed?"
2. **"We saw this last month, can someone find that JIRA?"** No
   memory between incidents. No fingerprint. No deduplication.
3. **"The logs are huge but I have no idea what to look at."**
   Telemetry without aggregation; engineers default to grep.
4. **"We fixed it but it's going to come back."** No feedback into
   future launches. Each fix is one-shot.
5. **"Postmortems take all afternoon."** Hand-rolled markdown,
   screenshots from rqt, console pastes. No artifact a customer or
   compliance team would accept.
6. **"Field robots have flaky failures we can't reproduce on the
   bench."** Without a bundle, the bench engineer has nothing to
   replay.

## 6. Why now (sharp version)

- ROS 2 has crossed the production threshold. Humble + Jazzy + Iron
  are running on real hardware; teams are filing real bug reports.
- The robotics market is moving from "can the robot move" to "can
  the robot stay in service for a quarter without an engineer in
  the field." Reliability is now the competitive axis. The current
  toolchain (foxglove, plotjuggler, rqt, ros2 bag) is built for
  *visualizing* data, not for *resolving* failures.
- There is no widely-adopted incident-bundle format for ROS 2. If
  we ship a credible one, the network effect is asymmetric: every
  bundle a team writes is leverage for the next bundle.
- LLM-assisted summarization has matured to the point where, given
  *structured evidence*, it can usefully draft a likely-cause
  narrative. (We use this conservatively, never as the source of
  truth, always over the bundle.)
- Founders building robots are increasingly ex-cloud-SRE engineers.
  They expect tooling that produces an artifact they can attach to
  a ticket. That artifact does not exist for ROS 2 today.

## 7. Differentiators

What competitors have that we *don't try to replicate*:

- Foxglove's beautiful visualization → not our game; the bundle
  is the artifact, not a chart.
- Datadog/Grafana dashboards → not our game; we are local-first,
  artifact-first.
- ros2 bag → orthogonal; we *attach* bags to incidents, we don't
  replace them.

What we have that they don't:

1. **Incident as a domain object.** Bundles are reproducible,
   diffable, attachable.
2. **Config and version signatures pinned to every incident.**
   "What changed?" answered automatically.
3. **A failure fingerprint that is stable across runs.** Two
   incidents that are "the same" are flagged as the same.
4. **A prevention loop.** The closed incident becomes a preflight
   check on the next launch.
5. **No cloud dependency.** The robot can be off-network and the
   product still works.
6. **Robotics-native semantics.** QoS profiles, ROS graph state,
   GPU thermal: first-class citizens, not log lines.

## 8. What this is *not*

- **Not Datadog for robots.** No hosted dashboard. No SaaS data
  ingestion.
- **Not Sentry for robots.** Sentry catches application
  exceptions; we capture *system-level* failures spanning ROS,
  process, and hardware.
- **Not a foxglove replacement.** Foxglove visualises; we
  reconstruct and recommend.
- **Not a fleet manager.** No remote control, no OTA. (We may
  *integrate* with one later.)
- **Not an AI-powered insights engine.** If we render an LLM
  summary, it sits *on top of* the structured bundle, never as the
  source. The artifact is auditable without the LLM.

## 9. Anti-positioning (specifically what we will not say)

We will not write or accept any of the following copy:

- "Real-time AI insights into your robotics fleet."
- "ChatOps for robotics."
- "Single pane of glass for robot reliability."
- "Cloud-native observability for ROS 2."
- "DevOps for robots."
- "Mission-critical." (Empty signal phrase.)
- "Enterprise-grade." (Empty signal phrase.)

We will instead say things like:

- "When a ROS 2 robot fails, BlackBoxRS produces a reproducible
  incident bundle in under five seconds."
- "Every incident becomes a preflight rule the next launch runs
  automatically."
- "Local-first. The bundle is the artifact."

## 10. Elevator pitch (45 seconds)

> Field robotics teams running ROS 2 lose hours per week to "why
> isn't this running like yesterday?" Logs aren't an answer; they're
> raw material. BlackBoxRS captures the same telemetry you'd already
> collect, but when something fails it produces a reproducible
> incident bundle: timeline, evidence, config and version
> signatures, a likely-cause narrative grounded in the evidence,
> and a preflight rule the next launch runs to keep that exact
> failure from happening again. It's local-first, ROS-2-native,
> and the bundle is portable. Postmortems collapse from an
> afternoon to a paragraph.

## 11. Hand-off checklist for marketing surfaces

When something is going on a website / GitHub / pitch deck:

- [ ] Has the word "incident" or "failure" in the headline.
- [ ] Has a concrete artifact in the first viewport (screenshot of
      `report.md`, terminal output, or bundle tree).
- [ ] Mentions ROS 2 explicitly in the first 50 words.
- [ ] Does not contain any phrase from the anti-positioning list.
- [ ] Mentions "local-first" or "bundle is the artifact" at least
      once.
- [ ] Links to a real bundle in `examples/incidents/`.

If any of those is false, send it back.
