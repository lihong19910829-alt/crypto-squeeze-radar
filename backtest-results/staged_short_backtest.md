# 阶段化做空策略回测

样本：603 条，交易对：1 个

## 数据限制
- 现有数据库为小时级快照；price 被用作小时收盘/开盘代理。
- 没有逐小时成交量时，成交量条件使用 quote_volume_24h 的20小时均值代理。
- 没有15分钟数据和OHLC时，版本C CHOCH不可计算；版本B使用价格跌破信号价格代理。
- 未设置止损止盈；按持仓小时数定时退出，收益未扣手续费/滑点。

## Top 10 参数组合

|排名|组合|交易次数|胜率|平均收益|平均亏损|盈亏比|最大回撤|最大连续亏损|平均持仓|Profit Factor|
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|1|p20_oi10_fund0.05_stall5_vol80_A_6h|0|None|None|None|None|None|0|None|None|
|2|p20_oi10_fund0.05_stall5_vol80_A_12h|0|None|None|None|None|None|0|None|None|
|3|p20_oi10_fund0.05_stall5_vol80_A_24h|0|None|None|None|None|None|0|None|None|
|4|p20_oi10_fund0.05_stall5_vol80_A_36h|0|None|None|None|None|None|0|None|None|
|5|p20_oi10_fund0.05_stall5_vol80_A_48h|0|None|None|None|None|None|0|None|None|
|6|p20_oi10_fund0.05_stall5_vol80_B_6h|0|None|None|None|None|None|0|None|None|
|7|p20_oi10_fund0.05_stall5_vol80_B_12h|0|None|None|None|None|None|0|None|None|
|8|p20_oi10_fund0.05_stall5_vol80_B_24h|0|None|None|None|None|None|0|None|None|
|9|p20_oi10_fund0.05_stall5_vol80_B_36h|0|None|None|None|None|None|0|None|None|
|10|p20_oi10_fund0.05_stall5_vol80_B_48h|0|None|None|None|None|None|0|None|None|

## 亏损最大的20笔交易

|组合|交易对|信号时间|入场|出场|收益|失败线索|
|---|---|---|---|---|---:|---|
