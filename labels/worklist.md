# Virta - labelling worklist
#
# The labelling pass writes here; you answer whenever you like; the next pass
# acts on your answers. It never changes anything you write.
#
# Answer on the `your_label:` line of a cluster:
#   your_label: fridge          -> a clean name: promoted to profiles/fridge.json
#   your_label: it's the dryer but only the hot cycle, ...
#                               -> a comment: sent to Nemotron, which proposes a
#                                  label in `llm_reply`; confirm with a clean name
#   your_label: ignore          -> rejected, never proposed again
#   your_label: later           -> needs more data, kept open
#
# Nothing here is ever acted on without your answer (propose, then confirm).

## Timeframe labels
# Ran something on purpose? One line each, the pass finds the step it made:
#   2026-09-16 07:42-07:44 kettle


## Recurring unknown loads

[B-1530W-25s]  +1532 W  phase B  ~24 s  10x in 1 d (1 session)  day 4, evening 6  PULSING (up to 10/session)
  seen: Wed 16.09 16:49, Wed 16.09 16:50, Wed 16.09 16:52, Wed 16.09 16:57, Wed 16.09 17:14, Wed 16.09 17:19 ...
  llm_guess: space_heater (70%)
  llm_why: 1532W on phase B, 24s pulsing cycles (thermostat), 10 occurrences in one evening session, Wednesday use, likely a resistive space heater with temperature control.
  your_label: 
  status: proposed

[B-160W-3m]  +162 W  phase B  ~3 min  4x in 1.2 d (2 sessions)  night 1, evening 3
  seen: Wed 16.09 20:14, Wed 16.09 20:29, Thu 17.09 22:50, Fri 18.09 00:18
  llm_guess: bathroom_extractor (40%)
  llm_why: 162W on phase B, 3-min non-pulsing sessions, evenings/night and weekdays, likely a briefly activated ventilation device.
  your_label: 
  status: proposed

[C-440W-36m]  +443 W  phase C  ~36 min  4x in 1.5 d (4 sessions)  morning 2, evening 2
  seen: Wed 16.09 20:51, Thu 17.09 07:45, Thu 17.09 18:35, Fri 18.09 07:44
  llm_guess: infrared_heat_lamp (50%)
  llm_why: 443W on phase C, 36-min non-pulsing sessions, morn/eve weekdays, likely a bathroom or reptile infrared heat lamp used briefly twice daily.
  your_label: 
  status: proposed

## Timeframe results
# written by the pass - one line per timeframe label above
(none yet)
