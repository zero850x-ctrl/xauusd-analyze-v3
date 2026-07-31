"""One-off mentor trade analysis from Jul 2026 screenshots.

Run: python scripts/mentor_trades.py

Dedupes by ticket (keeps max-lot row). Broker hour = open-time hour as displayed
in screenshots (assumed broker-local; cross-check vs BROKER_UTC_OFFSET_HOURS).

Hour constants mirror analyze_v3.py. RAW_TRADES covers Jul 14-17 (68 deduped).
EXTENDED_RAW_TRADES is a placeholder for Jul 20-24 rows — add sanitized tickets
there to reproduce 126-sample stats in-repo.
"""
import collections
import statistics
import sys
from datetime import datetime

GOLDEN_HOURS = {9}
ADVISORY_HOURS_0408 = {4, 5, 6, 8}
ADVISORY_HOUR_1700 = {17}
DANGER_HOURS = set()  # no hard-block hours (synced with analyze_v3.py)
DANGER_ADVISORY_HOURS = ADVISORY_HOURS_0408 | ADVISORY_HOUR_1700

RAW_TRADES = [
    # idx=1 dropped: duplicate ticket 28478941 (see idx=9, kept max lot)
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

# Jul 20-24 mentor screenshots — paste sanitized rows here to audit 126-sample stats.
EXTENDED_RAW_TRADES = [
    # Jul 29 screenshot (5 trades) + Jul 29-30 screenshots (65 trades) = 70 extended rows total
    dict(idx=70, tk=29040009, side="B", lot=0.31, op=4082.30, cp=4078.31, ot="07-29 19:13", ct="07-29 19:19", sl=4071.64, tp=None, pnl=-123.57),
    dict(idx=71, tk=29040524, side="S", lot=0.06, op=4072.64, cp=4071.66, ot="07-29 19:19", ct="07-29 19:20", sl=None,    tp=None, pnl=5.88),
    dict(idx=72, tk=29040622, side="B", lot=0.06, op=4070.57, cp=4068.42, ot="07-29 19:20", ct="07-29 19:23", sl=4067.64, tp=None, pnl=-12.90),
    dict(idx=73, tk=29040861, side="B", lot=0.06, op=4067.36, cp=4064.90, ot="07-29 19:24", ct="07-29 19:25", sl=4064.90, tp=None, pnl=-14.76),
    dict(idx=74, tk=29041087, side="B", lot=0.06, op=4066.90, cp=4066.76, ot="07-29 19:28", ct="07-29 19:31", sl=4063.70, tp=None, pnl=-0.82),
    # Jul 29-30 screenshots — 65 new trades (13 screenshots, 0 duplicates)
    dict(idx=75, tk=29030385, side="B", lot=0.03, op=4012.34, cp=4010.84, ot="07-29 16:49", ct="07-29 17:13", sl=4006.8, tp=None, pnl=-4.5),
    dict(idx=76, tk=29031175, side="S", lot=0.03, op=4011.03, cp=4016.0, ot="07-29 17:13", ct="07-29 17:23", sl=4016.0, tp=None, pnl=-14.91),
    dict(idx=77, tk=29032133, side="B", lot=0.01, op=4016.36, cp=4031.39, ot="07-29 17:45", ct="07-29 18:02", sl=4007.65, tp=None, pnl=15.03),
    dict(idx=78, tk=29033274, side="B", lot=0.02, op=4032.51, cp=4061.99, ot="07-29 18:03", ct="07-29 19:02", sl=None, tp=None, pnl=58.96),
    dict(idx=79, tk=29038487, side="S", lot=0.01, op=4077.31, cp=4082.67, ot="07-29 19:02", ct="07-29 19:13", sl=None, tp=None, pnl=-5.36),
    dict(idx=80, tk=29041468, side="S", lot=0.03, op=4062.06, cp=4056.75, ot="07-29 19:31", ct="07-29 19:33", sl=None, tp=None, pnl=15.93),
    dict(idx=81, tk=29041816, side="B", lot=0.02, op=4061.02, cp=4095.99, ot="07-29 19:33", ct="07-29 19:54", sl=4041.15, tp=None, pnl=69.94),
    dict(idx=82, tk=29042172, side="S", lot=0.01, op=4064.65, cp=4065.76, ot="07-29 19:38", ct="07-29 19:38", sl=None, tp=None, pnl=-1.11),
    dict(idx=83, tk=29042202, side="B", lot=0.01, op=4066.23, cp=4078.64, ot="07-29 19:38", ct="07-29 19:41", sl=4058.43, tp=4089.0, pnl=12.41),
    dict(idx=84, tk=29043125, side="S", lot=0.03, op=4083.67, cp=4088.0, ot="07-29 19:45", ct="07-29 19:46", sl=4088.0, tp=None, pnl=-12.99),
    dict(idx=85, tk=29043270, side="S", lot=0.03, op=4085.9, cp=4091.36, ot="07-29 19:46", ct="07-29 19:47", sl=4091.36, tp=None, pnl=-16.38),
    dict(idx=86, tk=29043405, side="B", lot=0.03, op=4093.04, cp=4095.09, ot="07-29 19:47", ct="07-29 19:53", sl=4083.56, tp=4104.0, pnl=6.15),
    dict(idx=87, tk=29044323, side="S", lot=0.02, op=4104.6, cp=4100.42, ot="07-29 19:54", ct="07-29 20:09", sl=None, tp=None, pnl=8.36),
    dict(idx=88, tk=29045962, side="S", lot=0.01, op=4093.62, cp=4068.57, ot="07-29 20:10", ct="07-29 21:44", sl=4068.57, tp=None, pnl=25.05),
    dict(idx=89, tk=29047883, side="S", lot=0.01, op=4069.84, cp=4053.81, ot="07-29 20:48", ct="07-29 20:57", sl=4073.8, tp=None, pnl=16.03),
    dict(idx=90, tk=29048523, side="B", lot=0.03, op=4048.07, cp=4051.36, ot="07-29 21:00", ct="07-29 21:09", sl=4035.66, tp=None, pnl=9.87),
    dict(idx=91, tk=29048847, side="S", lot=0.02, op=4053.56, cp=4054.7, ot="07-29 21:09", ct="07-29 21:13", sl=4056.0, tp=4047.2, pnl=-2.28),
    dict(idx=92, tk=29048938, side="B", lot=0.03, op=4055.42, cp=4050.27, ot="07-29 21:13", ct="07-29 21:17", sl=4048.58, tp=None, pnl=-15.45),
    dict(idx=93, tk=29049032, side="S", lot=0.02, op=4050.91, cp=4053.16, ot="07-29 21:17", ct="07-29 21:29", sl=4057.0, tp=None, pnl=-4.49),
    dict(idx=94, tk=29049538, side="B", lot=0.02, op=4061.82, cp=4064.89, ot="07-29 21:38", ct="07-29 21:40", sl=4042.17, tp=None, pnl=6.14),
    dict(idx=95, tk=29049806, side="B", lot=0.02, op=4068.82, cp=4064.0, ot="07-29 21:48", ct="07-29 21:50", sl=4064.0, tp=4080.0, pnl=-9.64),
    dict(idx=96, tk=29049849, side="B", lot=0.02, op=4066.28, cp=4084.98, ot="07-29 21:51", ct="07-30 03:15", sl=4062.63, tp=None, pnl=37.4),
    dict(idx=97, tk=29058993, side="B", lot=0.02, op=4083.34, cp=4079.58, ot="07-30 03:29", ct="07-30 03:31", sl=4079.58, tp=None, pnl=-7.52),
    dict(idx=98, tk=29066912, side="S", lot=0.02, op=4031.61, cp=4034.27, ot="07-30 06:45", ct="07-30 06:47", sl=None, tp=None, pnl=-5.32),
    dict(idx=99, tk=29067054, side="B", lot=0.02, op=4034.42, cp=4036.28, ot="07-30 06:47", ct="07-30 07:56", sl=4007.12, tp=None, pnl=3.72),
    dict(idx=100, tk=29067241, side="S", lot=0.02, op=4034.58, cp=4041.0, ot="07-30 06:51", ct="07-30 08:01", sl=4041.0, tp=None, pnl=-12.84),
    dict(idx=101, tk=29068462, side="S", lot=0.12, op=4031.0, cp=4036.62, ot="07-30 07:28", ct="07-30 07:39", sl=None, tp=4023.7, pnl=-67.44),
    dict(idx=102, tk=29068805, side="B", lot=0.12, op=4037.08, cp=4035.12, ot="07-30 07:39", ct="07-30 07:40", sl=4033.95, tp=None, pnl=-23.52),
    dict(idx=103, tk=29068835, side="S", lot=0.12, op=4035.54, cp=4038.14, ot="07-30 07:40", ct="07-30 07:42", sl=None, tp=None, pnl=-31.2),
    dict(idx=104, tk=29068874, side="B", lot=0.12, op=4038.15, cp=4035.63, ot="07-30 07:42", ct="07-30 07:48", sl=4033.94, tp=None, pnl=-30.2),
    dict(idx=105, tk=29069011, side="S", lot=0.08, op=4035.72, cp=4040.51, ot="07-30 07:49", ct="07-30 08:01", sl=4040.75, tp=None, pnl=-38.29),
    dict(idx=106, tk=29069037, side="S", lot=0.02, op=4036.74, cp=4038.84, ot="07-30 07:50", ct="07-30 07:51", sl=4038.84, tp=None, pnl=-4.2),
    dict(idx=107, tk=29069180, side="B", lot=0.08, op=4039.84, cp=4037.55, ot="07-30 07:54", ct="07-30 07:56", sl=None, tp=None, pnl=-18.32),
    dict(idx=108, tk=29069435, side="B", lot=0.02, op=4043.01, cp=4043.26, ot="07-30 08:01", ct="07-30 08:02", sl=None, tp=None, pnl=0.5),
    dict(idx=109, tk=29069616, side="B", lot=0.02, op=4044.66, cp=4047.85, ot="07-30 08:03", ct="07-30 08:21", sl=4034.15, tp=None, pnl=6.37),
    dict(idx=110, tk=29070429, side="S", lot=0.02, op=4044.61, cp=4046.44, ot="07-30 08:21", ct="07-30 08:27", sl=4051.64, tp=None, pnl=-3.66),
    dict(idx=111, tk=29070710, side="S", lot=0.01, op=4044.09, cp=4041.37, ot="07-30 08:30", ct="07-30 08:35", sl=4048.44, tp=4039.95, pnl=2.72),
    dict(idx=112, tk=29070931, side="S", lot=0.01, op=4039.76, cp=4046.22, ot="07-30 08:36", ct="07-30 08:45", sl=None, tp=None, pnl=-6.46),
    dict(idx=113, tk=29071320, side="B", lot=0.02, op=4045.88, cp=4040.37, ot="07-30 08:46", ct="07-30 08:55", sl=None, tp=None, pnl=-11.02),
    dict(idx=114, tk=29071575, side="S", lot=0.02, op=4039.79, cp=4045.76, ot="07-30 08:56", ct="07-30 09:04", sl=4045.76, tp=None, pnl=-11.94),
    dict(idx=115, tk=29074345, side="B", lot=0.02, op=4069.51, cp=4063.18, ot="07-30 09:37", ct="07-30 09:41", sl=4063.18, tp=None, pnl=-12.66),
    dict(idx=116, tk=29074619, side="B", lot=0.02, op=4065.63, cp=4066.2, ot="07-30 09:42", ct="07-30 09:44", sl=4062.75, tp=None, pnl=1.14),
    dict(idx=117, tk=29074624, side="B", lot=0.02, op=4065.17, cp=4064.64, ot="07-30 09:42", ct="07-30 09:43", sl=4062.75, tp=None, pnl=-1.06),
    dict(idx=118, tk=29074716, side="B", lot=0.02, op=4067.18, cp=4065.39, ot="07-30 09:45", ct="07-30 09:48", sl=4065.39, tp=None, pnl=-3.58),
    dict(idx=119, tk=29074951, side="B", lot=0.02, op=4064.41, cp=4060.0, ot="07-30 09:49", ct="07-30 09:54", sl=4060.0, tp=None, pnl=-8.82),
    dict(idx=120, tk=29075764, side="B", lot=0.01, op=4067.3, cp=4064.74, ot="07-30 10:10", ct="07-30 10:41", sl=4059.45, tp=4082.74, pnl=-2.56),
    dict(idx=121, tk=29076060, side="B", lot=0.01, op=4070.5, cp=4066.08, ot="07-30 10:21", ct="07-30 10:30", sl=4066.08, tp=None, pnl=-4.42),
    dict(idx=122, tk=29076851, side="S", lot=0.01, op=4065.19, cp=4073.02, ot="07-30 10:41", ct="07-30 10:51", sl=4073.02, tp=None, pnl=-7.83),
    dict(idx=123, tk=29077127, side="B", lot=0.02, op=4069.62, cp=4076.5, ot="07-30 10:53", ct="07-30 12:32", sl=4076.5, tp=None, pnl=13.76),
    dict(idx=124, tk=29078345, side="B", lot=0.02, op=4076.17, cp=4081.1, ot="07-30 11:21", ct="07-30 11:36", sl=4072.8, tp=4081.1, pnl=9.86),
    dict(idx=125, tk=29081754, side="B", lot=0.01, op=4080.91, cp=4071.59, ot="07-30 13:26", ct="07-30 13:51", sl=4071.59, tp=None, pnl=-9.32),
    dict(idx=126, tk=29081809, side="B", lot=0.01, op=4080.1, cp=4071.59, ot="07-30 13:27", ct="07-30 13:51", sl=4071.59, tp=4090.0, pnl=-8.51),
    dict(idx=127, tk=29085899, side="S", lot=0.01, op=4078.28, cp=4077.8, ot="07-30 14:33", ct="07-30 14:33", sl=None, tp=None, pnl=0.48),
    dict(idx=128, tk=29085929, side="B", lot=0.01, op=4079.79, cp=4114.72, ot="07-30 14:34", ct="07-30 17:04", sl=4064.43, tp=None, pnl=34.93),
    dict(idx=129, tk=29079761, side="S", lot=0.01, op=4103.0, cp=4112.78, ot="07-30 14:54", ct="07-30 17:02", sl=None, tp=None, pnl=-9.78),
    dict(idx=130, tk=29098886, side="S", lot=0.01, op=4114.65, cp=4114.61, ot="07-30 17:04", ct="07-30 17:16", sl=None, tp=None, pnl=0.04),
    dict(idx=131, tk=29099395, side="B", lot=0.05, op=4114.12, cp=4114.31, ot="07-30 17:16", ct="07-30 17:34", sl=4113.0, tp=None, pnl=0.96),
    dict(idx=132, tk=29099804, side="S", lot=0.02, op=4115.99, cp=4105.0, ot="07-30 17:29", ct="07-30 18:38", sl=4119.5, tp=None, pnl=21.99),
    dict(idx=133, tk=29100217, side="S", lot=0.02, op=4109.75, cp=4108.72, ot="07-30 17:43", ct="07-30 17:46", sl=4116.66, tp=4102.0, pnl=2.07),
    dict(idx=134, tk=29101753, side="S", lot=0.01, op=4103.78, cp=4105.03, ot="07-30 18:52", ct="07-30 20:13", sl=4109.0, tp=None, pnl=-1.25),
    dict(idx=135, tk=29102035, side="S", lot=0.03, op=4102.58, cp=4105.47, ot="07-30 19:22", ct="07-30 20:13", sl=4108.63, tp=None, pnl=-8.67),
    dict(idx=136, tk=29102043, side="S", lot=0.01, op=4103.37, cp=4101.5, ot="07-30 19:23", ct="07-30 19:51", sl=None, tp=None, pnl=1.87),
    dict(idx=137, tk=29101503, side="S", lot=0.01, op=4099.6, cp=4104.05, ot="07-30 19:54", ct="07-30 20:12", sl=None, tp=None, pnl=-4.45),
    dict(idx=138, tk=29102539, side="B", lot=0.01, op=4105.11, cp=4110.12, ot="07-30 20:13", ct="07-30 20:54", sl=4098.44, tp=None, pnl=5.01),
    dict(idx=139, tk=29102543, side="B", lot=0.03, op=4105.46, cp=4110.54, ot="07-30 20:13", ct="07-30 20:55", sl=4109.37, tp=None, pnl=15.25),
]


def _log(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', 'replace').decode('ascii'))


def parse_ts(ts):
    return datetime.strptime("2026-" + ts, "%Y-%m-%d %H:%M")


def dedupe_trades(rows):
    """Keep one row per ticket (max lot). Returns (deduped, dropped_idx)."""
    by_ticket = {}
    dropped = []
    for row in rows:
        tk = row['tk']
        if tk not in by_ticket or row['lot'] > by_ticket[tk]['lot']:
            if tk in by_ticket:
                dropped.append(by_ticket[tk]['idx'])
            by_ticket[tk] = row
        else:
            dropped.append(row['idx'])
    return list(by_ticket.values()), dropped


def enrich(trades):
    for t in trades:
        opened, closed = parse_ts(t['ot']), parse_ts(t['ct'])
        t['hold_min'] = (closed - opened).total_seconds() / 60
        t['has_sl'] = t['sl'] is not None
        t['has_tp'] = t['tp'] is not None
        t['win'] = t['pnl'] > 0
        t['op_broker_hour'] = opened.hour


def hour_tag(hour):
    if hour in GOLDEN_HOURS:
        return '[G]'
    if hour in ADVISORY_HOURS_0408 or hour in ADVISORY_HOUR_1700:
        return '[A]'
    return '   '


def all_raw_trades():
    return RAW_TRADES + EXTENDED_RAW_TRADES


def main():
    trades, dropped = dedupe_trades(all_raw_trades())
    enrich(trades)
    if dropped:
        _log(f"NOTE: dropped duplicate ticket rows idx={dropped}")

    n = len(trades)
    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    _log(f"=== Mentor sample ({n} unique tickets; {len(EXTENDED_RAW_TRADES)} extended rows pending) ===")
    _log(f"Total PnL: {sum(t['pnl'] for t in trades):+.2f}")
    _log(f"Wins: {len(wins)} ({len(wins)/n*100:.1f}%)  Losses: {len(losses)}")
    if wins:
        _log(f"Avg win: {statistics.mean(t['pnl'] for t in wins):+.2f}")
    if losses:
        _log(f"Avg loss: {statistics.mean(t['pnl'] for t in losses):+.2f}")

    _log("\n=== SL/TP ===")
    for label, pred in [
        ('has SL', lambda t: t['has_sl']),
        ('no SL', lambda t: not t['has_sl']),
        ('has TP', lambda t: t['has_tp']),
        ('no TP', lambda t: not t['has_tp']),
    ]:
        sub = [t for t in trades if pred(t)]
        w = sum(1 for t in sub if t['win'])
        pnl = sum(t['pnl'] for t in sub)
        _log(f"{label}: {len(sub)} trades, win {w/len(sub)*100:.1f}%, net {pnl:+.2f}")

    _log("\n=== Open hour (broker-local assumed) ===")
    by_hour = collections.defaultdict(list)
    for t in trades:
        by_hour[t['op_broker_hour']].append(t)
    for hour in sorted(by_hour):
        sub = by_hour[hour]
        if len(sub) < 2:
            continue
        w = sum(1 for t in sub if t['win'])
        pnl = sum(t['pnl'] for t in sub)
        _log(f"{hour_tag(hour)} {hour:02d}:00  {len(sub):2d} trades, win {w/len(sub)*100:5.1f}%, net {pnl:+8.2f}")

    _log("\n=== Same-direction overlap (exploratory) ===")
    ts = sorted(trades, key=lambda t: parse_ts(t['ot']))
    pairs = []
    involved = {}
    for i in range(len(ts)):
        for j in range(i):
            a, b = ts[j], ts[i]
            if a['side'] != b['side']:
                continue
            if parse_ts(b['ot']) < parse_ts(a['ct']):
                pairs.append((a['idx'], b['idx'], a['side']))
                involved[a['idx']] = a
                involved[b['idx']] = b
    unique_pnl = sum(t['pnl'] for t in involved.values())
    _log(f"Overlap pairs: {len(pairs)} (pair-sum PnL is inflated; do not use for decisions)")
    _log(f"Unique trades in any overlap: {len(involved)}, net PnL {unique_pnl:+.2f}")


if __name__ == '__main__':
    main()
