# BlackBoxRS: Pivot Brief

Status: design doc. Authored 2026-05-07. Supersedes the "flight recorder /
observability daemon" framing in the current README.

## 1. Diagnosis: why the current product is not enough

Current BlackBoxRS is a **single-host telemetry logger with four anomaly
detectors and a JSONL stream**. That is technically respectable. It is also
not a product anyone would pay for.

Concretely, the gaps:

- **No artifact a robotics engineer would attach to a bug report.** Today the
  output is a rotating pile of JSONL lines. To investigate a failure you
  `dump-log`, scroll, and reason in your head. There is no incident, no
  bundle, no reproducible package.
- **No causal model of what happened.** Anomalies fire independently. There
  is no concept of "this anomaly *and* that crash *and* this config change
  are one incident."
- **No memory across runs.** Two failures with the same root cause look
  identical to a human and totally distinct to BlackBoxRS. Fingerprints,
  clusters, prevention rules: none exist.
- **No prevention loop.** Even when an incident is understood, nothing
  prevents the next launch from hitting the same wall.
- **Wrong center of gravity for buyers.** "JSONL telemetry daemon" is in the
  same category as Prometheus, ros2-bag, foxglove logs, and a hundred ad-hoc
  bash scripts. Robotics teams already have logs. They do not have answers.
- **Single-host scope is fine, but presented as the product.** Single-host is
  the right starting point. But framing the product as "single-host
  observability" voluntarily caps the imagination of a buyer.

The wedge is not "see the data." It is **"resolve the incident."**

## 2. New product thesis

> When a ROS 2 robot fails, BlackBoxRS produces a reproducible incident
> report: timeline, evidence, likely cause, and a prevention check the
> next launch will run automatically.

Four-stage loop:

1. **Observe**: keep the existing telemetry capture. Treat it as the
   evidence substrate, not the product.
2. **Explain**: when something breaks, package the evidence into an
   *incident bundle* with timeline, snapshots, config/version signatures,
   and a likely-cause narrative grounded in the bundle.
3. **Replay**: emit a self-contained artifact (bundle directory, optional
   compressed archive) that another engineer can open, read, and re-derive
   the same conclusions. Bundle is the unit of currency.
4. **Prevent**: every closed incident can produce a `PreventionRule` that
   becomes a preflight check. Future launches refuse to start (or warn
   loudly) when the same precursor is detected.

The product is not "a logger." The product is the **incident** as a first-class
domain object, and the **prevention** that follows from it.

## 3. Who this is for

Primary ICPs (in order):

1. **Field robotics teams** running ROS 2 on real hardware (delivery,
   inspection, agricultural, last-mile, defense-adjacent civil). They have
   robots failing in flaky ways and a Slack channel full of "anyone seen
   this before?" Their pain is *recurrence*.
2. **Robotics platform / reliability engineers** at companies with 5+
   robots in the field. They own postmortems and they own the launch
   readiness review. Bundle + prevention rule = their workflow, today done
   in JIRA tickets.
3. **Robotics research labs and small startups** running ROS 2 on
   GO2 / Spot / TurtleBot / custom platforms. They lose hours per week to
   "why isn't this running like yesterday." They need a postmortem
   substrate and they have no SRE team.

Not the ICP yet:
- Cloud-scale fleet operators wanting hosted dashboards. (Right product,
  wrong order. Build the bundle first, the dashboard second.)
- Industrial ROS 1 sites. ROS 2 only.
- Pure simulation-only users. The wedge is real-hardware failures.

## 4. The painful workflow we own

Today, when a ROS 2 robot fails on a field test:

1. Engineer SSHs in, `ros2 topic list`, `ros2 node list`, scrolls journalctl,
   greps. Time spent: 20–90 minutes per incident.
2. They paste log fragments into Slack. Three other engineers compare notes
   from memory.
3. The "fix" is a one-line config change with no record of *why* and no test.
4. The same failure recurs on a different robot two weeks later. Nobody
   remembers the original.

After BlackBoxRS:

1. Robot fails. Daemon was running. `robot-blackbox incident build`.
2. Output: an `incident_<id>/` directory with `report.md`, `incident.json`,
   `timeline.json`, `evidence/`, `signatures/`, `attachments/` (optional bag).
3. Engineer reads `report.md` for 5 minutes. Likely cause is named with
   confidence and the supporting evidence is hyperlinked.
4. They convert it to a `PreventionRule`. Next launch on any robot runs
   `robot-blackbox preflight` and the rule fires before the failure.

That loop is the workflow we own. Telemetry is the substrate; the *bundle*
and the *prevention* are the product.

## 5. Why this is more valuable than generic observability

- **Generic observability** answers "what is the value of metric X right
  now." BlackBoxRS answers "what failed, why, and how do we keep it from
  happening again."
- Generic observability assumes you already know which metric to look at.
  Robotics failures cross subsystems (a thermal spike causes a TF gap which
  causes a planner stall). The relevant metric is the *correlation*, not
  any single signal.
- Generic observability outputs charts. Nobody attaches a Grafana panel to
  a bug. Engineers attach **artifacts**. The bundle is the artifact.
- Robotics is config-heavy, version-heavy, and hardware-coupled. None of
  the cloud SaaS observability tools capture launch files, parameter
  YAMLs, URDFs, package versions, or kernel/driver state. BlackBoxRS does.

## 6. The wedge

> One command produces a credible incident bundle from data that was already
> being captured.

Specifically, v0.4 is the first cut where a user can do:

```
robot-blackbox incident build --since 10m
# → ~/.blackboxrs/incidents/inc_2026-05-07T14-22-13_a3f2/
#   ├── report.md
#   ├── incident.json
#   ├── timeline.json
#   ├── signatures/
#   │   ├── config.json
#   │   └── versions.json
#   └── evidence/
#       ├── events.jsonl
#       └── snapshots.json
```

Everything else (clustering, preflight automation, prevention library) is
downstream of getting that single artifact right.

## 7. What not to build right now

Explicitly out of scope for this pivot:

- **Cloud upload / hosted dashboard / multi-robot aggregation.** The bundle
  is local-first. Anything else later.
- **ML-based root-cause classification.** We will get a long way with
  rules, fingerprints, and temporal correlation. Save ML for after we have
  100s of bundles.
- **Generic SaaS framing.** No "AI-powered insights." If we use LLMs, it
  will be opt-in narrative generation over a structured bundle, never as
  the source of evidence.
- **A web UI in v0.4.** The artifact is the UI. The website renders the
  artifact later.
- **TF tree introspection, service/action introspection, distributed
  bus.** Each is a real ask but each is a quarter of work and none of
  them is the wedge.
- **Rewriting the JSONL store.** It is fine. Treat it as the evidence
  bus.
- **Multi-process / multi-host capture.** Single-host first; the
  incident format is forward-compatible.

## 8. Top three demo scenarios

(See `DEMO_PLAN.md` for the full scenario library; these are the three
that should be in any pitch.)

1. **TF tree breaks mid-mission.** A `static_transform_publisher` is killed
   during a navigation demo. Without BlackBoxRS, the planner just goes
   quiet. With BlackBoxRS, an incident bundle is produced naming the
   missing TF frame, the timestamped subscriber-count drop, and a
   prevention rule that asserts the frame is present at launch.
2. **QoS mismatch silently drops messages.** A reliability mismatch between
   publisher and subscriber on `/scan`. The classic "topic exists but no
   data flows." BlackBoxRS bundle highlights the QoS snapshot diff and
   produces a prevention rule that fails preflight when the mismatch
   reappears.
3. **Thermal-induced node dropout on Jetson.** GPU passes 80 °C, kswapd
   spikes, a perception node OOMs. BlackBoxRS correlates the thermal
   signal, the resource pressure, and the dead topic, and ranks "thermal
   throttle → node OOM" above unrelated coincident anomalies.

## 9. Top three reasons a robotics company would care

1. **MTTR collapses.** Incidents that take an afternoon turn into 15 min.
   This is real productivity, not vanity.
2. **Failures stop recurring.** Every closed incident becomes a preflight
   rule. The library compounds. After 6 months a team's preflight catches
   most launch-time regressions before the robot moves.
3. **Postmortems become a one-command output.** Compliance, customer
   escalations, and internal reviews all need the same artifact. Today
   that's a person hand-writing markdown. Tomorrow it's
   `incident build --tag customer-escalation`.

## 10. Risks and limitations

Technical:

- **Single-host scope.** The current capture is one process on one host.
  Multi-host robots need a forwarder, which is not in v0.4.
- **JSONL is not infinite.** The current rotation is size-bounded; very
  long pre-incident windows can be missing if the buffer rolled. We must
  document this and offer a config knob.
- **Likely-cause is heuristic, not proven.** Naming a cause has the same
  failure mode as a junior engineer guessing. We must report confidence
  and *always* surface the underlying evidence so the user can override.
- **Preflight rules are a foot-gun.** A poorly-written rule can block all
  launches. Rules must be pinned to fingerprint provenance and easy to
  disable.
- **TF / service / action coverage is missing.** A real robot incident
  often involves `/tf` semantics. We need at least topic-level visibility
  on `/tf` and `/tf_static` in v0.4 even if we do not yet do tree-shape
  analysis.

Product:

- **"Why doesn't this exist already?" cuts both ways.** It either means we
  found a gap, or it means there is a structural reason (legacy ROS 1
  inertia, every team rolling their own). We assume the gap is real
  because the existing tools (foxglove, plotjuggler, rqt) are visualizers,
  not incident systems.
- **Solo founder + Claude Code is enough for v0.4 but not for serious
  fleet support.** Stay narrow. Bundle first.
- **The bundle has to actually be useful.** If `report.md` reads like
  generic SaaS output, the product fails on first contact with a real
  engineer. The doc must be terse, technical, and accountable.

## 11. Verification of the pivot

We will know the pivot worked when:

- A working ROS 2 engineer can read a generated `report.md` cold and form
  a correct hypothesis without opening any other tool.
- A bundle reproduces the same `report.md` on another machine when fed
  back into BlackBoxRS.
- A `PreventionRule` produced from incident A fires preflight on robot B
  before failure A recurs.
- We can show this end-to-end in under 5 minutes in a live demo.

Anything less is not the pivot.
