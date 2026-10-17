from paths import RESULTS, CHARTS
import json, lab, engine
from charts import eq_chart
c = json.load(open(str(RESULTS / 'candidates.json')))
p = c['A: 12:00 iron condor, 10-delta, $10 wings, stop 2x credit']
ser = [(f'IV = {k:.2f} x VIX' + (' (base, ~VIX1D)' if k == 0.62 else ''), engine.run(lab.days(), **dict(p, _k=k))) for k in (0.70, 0.62, 0.55)]
ser.append(('base IV, 1.5x bid/ask', engine.run(lab.days(), **dict(p, _k=0.62, spread_mult=1.5))))
eq_chart(ser, '05_condor_sensitivity.png', 'Candidate A depends on how rich 0DTE options really are')
