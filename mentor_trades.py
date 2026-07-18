import statistics, collections
from datetime import datetime

trades = [
 dict(idx=1,  tk=28478941, side="S", lot=0.06, op=4059.39, cp=4039.75, ot="07-14 17:34", ct="07-15 02:46", sl=None, tp=None,     pnl=117.84),
 dict(idx=2,  tk=28478829, side="S", lot=0.03, op=4078.83, cp=4053.45, ot="07-14 17:29", ct="07-14 19:37", sl=None, tp=4036.47, pnl=76.14),
 dict(idx=3,  tk=28439157, side="B", lot=0.05, op=4020.53, cp=4018.93, ot="07-14 05:47", ct="07-14 11:07", sl=4013.18, tp=None,  pnl=-8.00),
 dict(idx=4,  tk=28433368, side="S", lot=0.02, op=3997.70, cp=4014.22, ot="07-14 02:38", ct="07-14 04:13", sl=4020, tp=None,    pnl=-33.04),
 dict(idx=5,  tk=28479762, side="S", lot=0.10, op=4058.41, cp=4061.00, ot="07-14 18:09", ct="07-14 18:23", sl=4061.00, tp=4040.00, pnl=-25.90),
 dict(idx=6,  tk=28479571, side="S", lot=0.06, op=4060.44, cp=4064.85, ot="07-14 18:04", ct="07-14 18:32", sl=4064.85, tp=None,   pnl=-26.46),
 dict(idx=7,  tk=28479091, side="S", lot=0.03, op=4060.64, cp=4062.25, ot="07-14 17:41", ct="07-14 17:43", sl=4062.25, tp=None,   pnl=-4.83),
 dict(idx=8,  tk=28479035, side="S", lot=0.03, op=4058.35, cp=4064.83, ot="07-14 17:37", ct="07-14 18:01", sl=4064.83, tp=4040.00, pnl=-19.44),
 dict(idx=9,  tk=28478941, side="S", lot=0.10, op=4059.48, cp=4038.99, ot="07-14 17:34", ct="07-15 03:02", sl=None, tp=None,     pnl=204.92),
 dict(idx=10, tk=28481059, side="S", lot=0.06, op=4058.82, cp=4058.85, ot="07-14 19:29", ct="07-15 01:59", sl=4060.80, tp=None,  pnl=-0.18),
 dict(idx=11, tk=28480940, side="B", lot=0.20, op=4062.98, cp=4058.16, ot="07-14 19:19", ct="07-14 19:24", sl=4058.16, tp=None,   pnl=-96.40),
 dict(idx=12, tk=28480528, side="B", lot=0.08, op=4062.36, cp=4061.00, ot="07-14 18:39", ct="07-14 18:41", sl=4061.00, tp=None,  pnl=-10.88),
 dict(idx=13, tk=28480377, side="B", lot=0.06, op=4064.27, cp=4063.31, ot="07-14 18:30", ct="07-14 18:32", sl=4062.14, tp=None,  pnl=-5.76),
 dict(idx=14, tk=28480227, side="B", lot=0.23, op=4061.17, cp=4059.84, ot="07-14 18:24", ct="07-15 03:02", sl=None, tp=None,     pnl=-30.52),
 dict(idx=15, tk=28497220, side="B", lot=0.02, op=4024.54, cp=4030.74, ot="07-15 07:21", ct="07-15 10:16", sl=4014.73, tp=None,  pnl=12.40),
 dict(idx=16, tk=28496438, side="B", lot=0.05, op=4033.72, cp=4062.54, ot="07-15 07:00", ct="07-15 18:29", sl=None, tp=None,    pnl=144.12),
 dict(idx=17, tk=28493059, side="B", lot=0.05, op=4032.20, cp=4023.10, ot="07-15 05:14", ct="07-15 06:30", sl=4023.10, tp=None,  pnl=-45.50),
 dict(idx=18, tk=28490915, side="B", lot=0.03, op=4040.38, cp=4037.19, ot="07-15 04:05", ct="07-15 04:09", sl=None, tp=None,    pnl=-9.57),
 dict(idx=19, tk=28490019, side="B", lot=0.10, op=4037.15, cp=4036.79, ot="07-15 03:39", ct="07-15 03:52", sl=4034.86, tp=None, pnl=-3.61),
 dict(idx=20, tk=28503549, side="B", lot=0.05, op=4032.80, cp=4029.42, ot="07-15 10:11", ct="07-15 10:16", sl=4029.42, tp=None, pnl=-16.90),
 dict(idx=21, tk=28501609, side="B", lot=0.03, op=4021.57, cp=4042.32, ot="07-15 09:15", ct="07-15 13:42", sl=4014.91, tp=None, pnl=62.25),
 dict(idx=22, tk=28501528, side="B", lot=0.02, op=4018.60, cp=4023.56, ot="07-15 09:14", ct="07-15 09:23", sl=None, tp=None,    pnl=9.92),
 dict(idx=23, tk=28500497, side="B", lot=0.02, op=4024.78, cp=4019.38, ot="07-15 08:48", ct="07-15 09:13", sl=4019.38, tp=None, pnl=-10.80),
 dict(idx=24, tk=28498150, side="B", lot=0.05, op=4033.08, cp=4022.37, ot="07-15 07:44", ct="07-15 08:27", sl=4022.37, tp=None, pnl=-53.55),
 dict(idx=25, tk=28533131, side="S", lot=0.15, op=4037.20, cp=4039.12, ot="07-15 18:10", ct="07-15 18:12", sl=4039.12, tp=None,  pnl=-28.80),
 dict(idx=26, tk=28526666, side="B", lot=0.15, op=4036.72, cp=4044.39, ot="07-15 17:17", ct="07-15 18:34", sl=4025.00, tp=None, pnl=115.11),
 dict(idx=27, tk=28521560, side="B", lot=0.06, op=4064.89, cp=4059.10, ot="07-15 15:07", ct="07-15 15:23", sl=4059.10, tp=None, pnl=-34.74),
 dict(idx=28, tk=28519264, side="S", lot=0.02, op=4060.18, cp=4044.67, ot="07-15 14:38", ct="07-15 16:15", sl=None, tp=None,   pnl=31.02),
 dict(idx=29, tk=28516387, side="S", lot=0.03, op=4071.94, cp=4063.54, ot="07-15 14:07", ct="07-15 18:58", sl=None, tp=None,   pnl=25.20),
 dict(idx=30, tk=28535877, side="B", lot=0.18, op=4061.98, cp=4073.89, ot="07-15 18:57", ct="07-15 19:32", sl=4060.26, tp=None, pnl=214.43),
 dict(idx=31, tk=28535669, side="B", lot=0.18, op=4063.79, cp=4061.36, ot="07-15 18:53", ct="07-15 18:56", sl=4061.36, tp=None, pnl=-43.74),
 dict(idx=32, tk=28534107, side="S", lot=0.08, op=4056.27, cp=4066.56, ot="07-15 18:35", ct="07-15 19:03", sl=4067.25, tp=None, pnl=-82.28),
 dict(idx=33, tk=28533596, side="S", lot=0.17, op=4038.06, cp=4042.27, ot="07-15 18:28", ct="07-15 18:29", sl=None, tp=None,  pnl=-71.57),
 dict(idx=34, tk=28533440, side="S", lot=0.22, op=4036.43, cp=4039.35, ot="07-15 18:22", ct="07-15 18:25", sl=4039.35, tp=None, pnl=-64.24),
 dict(idx=35, tk=28544429, side="S", lot=0.02, op=4047.25, cp=4040.33, ot="07-16 02:10", ct="07-16 02:18", sl=None, tp=None,  pnl=13.84),
 dict(idx=36, tk=28544231, side="S", lot=0.03, op=4043.73, cp=4040.99, ot="07-16 02:06", ct="07-16 12:54", sl=None, tp=None,   pnl=8.23),
 dict(idx=37, tk=28542954, side="S", lot=0.03, op=4055.67, cp=4061.10, ot="07-16 01:43", ct="07-16 01:52", sl=4061.10, tp=None, pnl=-16.29),
 dict(idx=38, tk=28537697, side="S", lot=0.08, op=4076.52, cp=4017.39, ot="07-15 19:32", ct="07-17 03:01", sl=4101.00, tp=None, pnl=473.03),
 dict(idx=39, tk=28536721, side="S", lot=0.08, op=4066.61, cp=4067.27, ot="07-15 19:13", ct="07-15 19:14", sl=None, tp=None,  pnl=-5.28),
 dict(idx=40, tk=28551972, side="B", lot=0.10, op=4034.98, cp=4032.47, ot="07-16 04:35", ct="07-16 05:09", sl=4032.47, tp=None, pnl=-25.10),
 dict(idx=41, tk=28549154, side="S", lot=0.10, op=4031.86, cp=4029.84, ot="07-16 03:36", ct="07-16 04:27", sl=4033.15, tp=None, pnl=20.25),
 dict(idx=42, tk=28548000, side="S", lot=0.10, op=4032.23, cp=4033.94, ot="07-16 03:14", ct="07-16 03:29", sl=4037.86, tp=None, pnl=-17.10),
 dict(idx=43, tk=28547590, side="S", lot=0.06, op=4033.20, cp=4033.97, ot="07-16 03:09", ct="07-16 03:29", sl=None, tp=None,  pnl=-4.62),
 dict(idx=44, tk=28547564, side="S", lot=0.06, op=4035.43, cp=4033.76, ot="07-16 03:09", ct="07-16 04:44", sl=4036.50, tp=None, pnl=10.00),
 dict(idx=45, tk=28578426, side="S", lot=0.03, op=3993.25, cp=3987.02, ot="07-16 13:55", ct="07-17 01:24", sl=4045.00, tp=None, pnl=18.69),
 dict(idx=46, tk=28560483, side="B", lot=0.20, op=4031.18, cp=4026.13, ot="07-16 08:48", ct="07-16 13:16", sl=4017.00, tp=None, pnl=-101.08),
 dict(idx=47, tk=28559553, side="S", lot=0.22, op=4024.79, cp=4027.39, ot="07-16 08:25", ct="07-16 08:47", sl=4030.00, tp=None, pnl=-57.28),
 dict(idx=48, tk=28558077, side="S", lot=0.10, op=4026.72, cp=4038.62, ot="07-16 07:44", ct="07-16 12:48", sl=4041.10, tp=None, pnl=-119.02),
 dict(idx=49, tk=28553163, side="S", lot=0.06, op=4033.16, cp=4030.26, ot="07-16 05:24", ct="07-16 08:48", sl=4040.10, tp=None, pnl=17.40),
 dict(idx=50, tk=28596219, side="S", lot=0.12, op=3994.75, cp=3994.00, ot="07-16 17:52", ct="07-17 04:18", sl=3997.06, tp=None, pnl=8.96),
 dict(idx=51, tk=28596076, side="S", lot=0.12, op=3989.75, cp=3992.92, ot="07-16 17:46", ct="07-16 17:50", sl=3992.92, tp=None, pnl=-38.04),
 dict(idx=52, tk=28595339, side="S", lot=0.06, op=3986.04, cp=3990.77, ot="07-16 17:25", ct="07-16 17:35", sl=3990.77, tp=None, pnl=-28.38),
 dict(idx=53, tk=28592355, side="S", lot=0.02, op=4012.01, cp=4008.36, ot="07-16 16:56", ct="07-16 16:56", sl=None, tp=None,   pnl=7.30),
 dict(idx=54, tk=28578497, side="S", lot=0.06, op=3994.54, cp=3990.37, ot="07-16 13:56", ct="07-17 01:24", sl=4030.00, tp=None, pnl=25.04),
 dict(idx=55, tk=28613229, side="S", lot=0.05, op=3980.63, cp=4016.00, ot="07-17 06:13", ct="07-17 19:29", sl=4016.00, tp=None, pnl=-176.85),
 dict(idx=56, tk=28606972, side="B", lot=0.08, op=3977.70, cp=3982.17, ot="07-17 03:50", ct="07-17 06:12", sl=3970.00, tp=None,  pnl=35.72),
 dict(idx=57, tk=28605663, side="B", lot=0.07, op=3982.64, cp=3975.29, ot="07-17 03:17", ct="07-17 03:36", sl=3975.29, tp=None, pnl=-51.45),
 dict(idx=58, tk=28603727, side="B", lot=0.15, op=3992.79, cp=3984.64, ot="07-17 02:15", ct="07-17 02:50", sl=None, tp=None,  pnl=-122.25),
 dict(idx=59, tk=28596529, side="S", lot=0.06, op=3991.45, cp=3988.26, ot="07-16 18:02", ct="07-17 01:24", sl=3997.20, tp=None, pnl=19.17),
 dict(idx=60, tk=28622902, side="B", lot=0.06, op=3999.80, cp=3998.45, ot="07-17 10:30", ct="07-17 10:41", sl=3995.00, tp=None, pnl=-8.10),
 dict(idx=61, tk=28619934, side="B", lot=0.06, op=3997.41, cp=3991.90, ot="07-17 09:07", ct="07-17 09:12", sl=3991.90, tp=None, pnl=-33.06),
 dict(idx=62, tk=28619268, side="B", lot=0.05, op=3995.65, cp=3993.07, ot="07-17 08:42", ct="07-17 08:57", sl=3993.07, tp=None, pnl=-12.90),
 dict(idx=63, tk=28618062, side="S", lot=0.03, op=3997.18, cp=3991.66, ot="07-17 07:53", ct="07-17 15:24", sl=4008.60, tp=None,  pnl=16.55),
 dict(idx=64, tk=28613282, side="S", lot=0.05, op=3979.73, cp=3997.83, ot="07-17 06:14", ct="07-17 07:21", sl=None, tp=None,  pnl=-90.48),
 dict(idx=65, tk=28645065, side="B", lot=0.22, op=4000.51, cp=4010.73, ot="07-17 15:58", ct="07-17 18:34", sl=3997.66, tp=None, pnl=224.74),
 dict(idx=66, tk=28644701, side="S", lot=0.22, op=3995.40, cp=3995.32, ot="07-17 15:52", ct="07-17 15:55", sl=3996.22, tp=None,  pnl=1.80),
 dict(idx=67, tk=28641264, side="B", lot=0.08, op=4000.20, cp=4004.82, ot="07-17 15:17", ct="07-17 16:58", sl=3987.60, tp=None,  pnl=36.93),
 dict(idx=68, tk=28639819, side="S", lot=0.05, op=3994.54, cp=4000.70, ot="07-17 15:02", ct="07-17 15:07", sl=4000.70, tp=3676.13, pnl=-30.80),
 dict(idx=69, tk=28636677, side="S", lot=0.01, op=3990.94, cp=3983.99, ot="07-17 14:39", ct="07-17 14:44", sl=None, tp=None,   pnl=6.95),
]

def parse(t):
    return datetime.strptime("2026-"+t, "%Y-%m-%d %H:%M")

for t in trades:
    o,c = parse(t['ot']), parse(t['ct'])
    t['hold_min'] = (c-o).total_seconds()/60
    t['has_sl'] = t['sl'] is not None
    t['has_tp'] = t['tp'] is not None
    t['win'] = t['pnl'] > 0

# broker UTC-3 → broker hour = UTC-3 ; screenshots appear to show broker local times
# (=open time as displayed). So we use the hour of ot as broker hour.
for t in trades:
    t['op_broker_hour'] = parse(t['ot']).hour

n=len(trades)
wins=[t for t in trades if t['win']]
losses=[t for t in trades if not t['win']]
print(f"=== 大樣本概覽 (前輪 5+本輪 64 = {n}) ===")
print(f"總 PnL: {sum(t['pnl'] for t in trades):+.2f}")
print(f"勝: {len(wins)} ({len(wins)/n*100:.1f}%)  敗: {len(losses)}")
print(f"勝均: {statistics.mean(t['pnl'] for t in wins):+.2f}  敗均: {statistics.mean(t['pnl'] for t in losses):+.2f}")
print(f"最大勝: {max(t['pnl'] for t in trades):+.2f}  最大敗: {min(t['pnl'] for t in trades):+.2f}")

print("\n=== 方向 ===")
for sd,name in [('B','BUY'),('S','SELL')]:
    sub=[t for t in trades if t['side']==sd]
    w=[t for t in sub if t['win']]
    print(f"{name}: {len(sub)} 筆, 勝 {len(w)}/{len(sub)} ({len(w)/len(sub)*100:.1f}%), 淨 {sum(t['pnl'] for t in sub):+.2f}")

print("\n=== SL/TP 有無 ===")
def grp(label, key, val):
    sub=[t for t in trades if t[key]==val]
    w=[t for t in sub if t['win']]
    pnl=sum(t['pnl'] for t in sub)
    print(f"{label}: {len(sub)} 筆, 勝 {len(w)} ({len(w)/len(sub)*100:.1f}%), 淨 {pnl:+.2f}, avg {pnl/len(sub):+.2f}")
grp("有 SL  ", 'has_sl', True)
grp("無 SL  ", 'has_sl', False)
grp("有 TP  ", 'has_tp', True)
grp("無 TP  ", 'has_tp', False)

print("\n=== 持倉時間 bucket ===")
buckets = [(0,5,'<5m'),(5,15,'5-15m'),(15,60,'15-60m'),(60,240,'1-4h'),(240,9999,'>4h')]
for lo,hi,label in buckets:
    sub=[t for t in trades if lo<=t['hold_min']<hi]
    if not sub: continue
    w=[t for t in sub if t['win']]
    pnl=sum(t['pnl'] for t in sub)
    print(f"{label:8}: {len(sub):2d} 筆, 勝率 {len(w)/len(sub)*100:5.1f}%, 淨 {pnl:+8.2f}, avg {pnl/len(sub):+7.2f}")

print("\n=== 開倉時段 (broker local hour) ===")
hr_b = collections.defaultdict(list)
for t in trades:
    hr_b[t['op_broker_hour']].append(t)
for h in sorted(hr_b):
    sub=hr_b[h]
    if len(sub)<2: continue
    w=[t for t in sub if t['win']]
    pnl=sum(t['pnl'] for t in sub)
    star = '🌅' if h in {1,9} else '🚫' if h in {17} else '  '
    print(f"{star} {h:02d}:00  {len(sub):2d} 筆, 勝 {len(w)/len(sub)*100:5.1f}%, 淨 {pnl:+8.2f}, avg {pnl/len(sub):+7.2f}")

print("\n=== lot bucket ===")
lb = [(0,0.03,'0.01-0.02'),(0.03,0.07,'0.03-0.06'),(0.07,0.16,'0.07-0.15'),(0.16,9,'0.16+')]
for lo,hi,label in lb:
    sub=[t for t in trades if lo<=t['lot']<hi]
    if not sub: continue
    w=[t for t in sub if t['win']]
    pnl=sum(t['pnl'] for t in sub)
    print(f"{label:10}: {len(sub):2d} 筆, 勝 {len(w)/len(sub)*100:5.1f}%, 淨 {pnl:+8.2f}, avg {pnl/len(sub):+7.2f}")

print("\n=== 日別 (open date) ===")
day=collections.defaultdict(list)
for t in trades:
    day[t['ot'][:5]].append(t)
for d in sorted(day):
    sub=day[d]
    if len(sub)<2: continue
    w=[t for t in sub if t['win']]
    pnl=sum(t['pnl'] for t in sub)
    print(f"{d}: {len(sub):2d} 筆, 勝 {len(w)/len(sub)*100:5.1f}%, 淨 {pnl:+8.2f}")

print("\n=== 重疊入場 (某單開倉在前單持倉中) ===")
# 排序按開倉時間
ts=sorted(trades, key=lambda t: parse(t['ot']))
# Detect same-direction stacking: open within prior same-direction close
overlaps=[]
for i in range(len(ts)):
    for j in range(i):
        a,b=ts[j],ts[i]
        if a['side']!=b['side']: continue
        if parse(b['ot']) < parse(a['ct']):  # b 開倉時 a 仍在場
            pairs = '+' if (a['win'] and b['win']) else '-' if (not a['win'] and not b['win']) else 'M'
            overlaps.append((a['idx'],b['idx'],a['side'],pairs,a['pnl']+b['pnl']))
total=sum(x[4] for x in overlaps)
both_w=sum(1 for x in overlaps if x[3]=='+')
print(f"同向重疊對數: {len(overlaps)}, 雙勝 {both_w}, 淨 PnL {total:+.2f}")
for o in overlaps[:20]:
    print(f"  #{o[0]}+#{o[1]} {o[2]} avg {o[4]/2:+.2f}")

print("\n=== >4h 持倉 (放飛型) ===")
for t in trades:
    if t['hold_min']>=240:
        print(f"  #{t['idx']:2d} {t['side']} lot{t['lot']:.2f} hold {t['hold_min']/60:.1f}h SL{'Y' if t['has_sl'] else 'N'} TP{'Y' if t['has_tp'] else 'N'} pnl {t['pnl']:+.2f}")