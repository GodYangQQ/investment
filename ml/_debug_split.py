"""临时调试时间切分"""
import pandas as pd
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ml.dataset import FEATURES_PARQUET, BENCHMARK_PARQUET, FEATURE_COLS, SEQ_LEN, FUTURE_DAYS, TRAIN_END, VAL_END, TEST_END

df = pd.read_parquet(FEATURES_PARQUET)
df["date"] = pd.to_datetime(df["date"])
print("数据日期范围:", df["date"].min(), "~", df["date"].max())
print("股票数:", df["code"].nunique())
print("SEQ_LEN:", SEQ_LEN, "FUTURE_DAYS:", FUTURE_DAYS)
print("TRAIN_END:", TRAIN_END, "VAL_END:", VAL_END, "TEST_END:", TEST_END)
print()

for label, start, end in [
    ("train", pd.Timestamp("2022-01-01"), TRAIN_END),
    ("val", TRAIN_END + pd.Timedelta(days=1), VAL_END),
    ("test", VAL_END + pd.Timedelta(days=1), TEST_END),
]:
    mask = (df["date"] >= start) & (df["date"] <= end)
    sub = df[mask]
    print(f"{label}: {len(sub)} rows, {sub['code'].nunique()} stocks, "
          f"dates {sub['date'].min()} ~ {sub['date'].max()}")
    # 检查有多少股票能形成有效样本
    valid = 0
    for code, grp in sub.groupby("code"):
        if len(grp) >= SEQ_LEN + FUTURE_DAYS:
            valid += 1
    print(f"  -> 有效股票(>={SEQ_LEN+FUTURE_DAYS}天): {valid}")
    print()
