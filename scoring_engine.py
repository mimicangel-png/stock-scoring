#!/usr/bin/env python3
"""
SS-Enhanced 评分引擎 + 邮件报告
策略: ≥75强烈买入, ≥70逢低买入, ≥60持有, ≥45观望, <45回避, <40连续3天强制卖出
"""
import json, math, urllib.request, sys, os
from datetime import datetime, timedelta

# ========== 加载 .env 配置（避免硬编码敏感信息）==========
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
_load_env()

# ========== 数据获取 ==========
def fetch_kline_batch(codes, days=130):
    all_kline = {}
    for idx, code in enumerate(codes):
        if code.startswith(("6", "9")): sym = f"sh{code}"
        else: sym = f"sz{code}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode("utf-8"))
            klines = data.get("data", {}).get(sym, {}).get("qfqday", []) or \
                     data.get("data", {}).get(sym, {}).get("day", [])
            if klines:
                all_kline[code] = [{'date':k[0],'open':float(k[1]),'close':float(k[2]),
                    'high':float(k[3]),'low':float(k[4]),'volume':float(k[5]) if len(k)>5 else 0} for k in klines]
        except: pass
        if (idx+1)%30==0: print(f"  K线: {idx+1}/{len(codes)}")
    return all_kline

def fetch_extra_info(codes):
    prefixed = [f"sh{c}" if c.startswith(("6","9")) else f"sz{c}" for c in codes]
    info = {}
    for i in range(0, len(prefixed), 60):
        batch = prefixed[i:i+60]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            for line in resp.read().decode("gbk").strip().split(";"):
                if "=" not in line or '"' not in line: continue
                vals = line.split('"')[1].split("~")
                if len(vals) < 55: continue
                code = line.split("=")[0].split("_")[-1][2:]
                info[code] = {"name":vals[1],"price":float(vals[3]) if vals[3] else 0,
                    "change_pct":float(vals[32]) if vals[32] else 0,
                    "pe_ttm":float(vals[39]) if vals[39] else 0,
                    "pb":float(vals[46]) if vals[46] else 0,
                    "mcap":float(vals[44]) if vals[44] else 0,
                    "turnover":float(vals[38]) if vals[38] else 0,
                    "vol_ratio":float(vals[49]) if vals[49] else 0}
        except: pass
    return info

# ========== 技术指标 ==========
def calc_ma(p,n):
    if len(p)<n: return None
    return sum(p[-n:])/n
def calc_ema(p,n):
    if len(p)<n: return None
    k=2/(n+1); ema=p[-n]
    for v in p[-n+1:]: ema=v*k+ema*(1-k)
    return ema
def calc_rsi(c,n=14):
    if len(c)<n+1: return 50
    g=sum(max(0,c[i]-c[i-1]) for i in range(-n,0))
    l=sum(max(0,c[i-1]-c[i]) for i in range(-n,0))
    if l==0: return 100
    return 100-100/(1+g/l)

# ========== SS-Enhanced 评分 ==========
def score_ss_enhanced(klines, idx):
    """SS-Enhanced 评分引擎，返回评分 + 各维度分值 + 加减分因子明细"""
    if idx < 60: return None
    w = klines[:idx+1]
    c=[k['close'] for k in w]; v=[k['volume'] for k in w]
    h=[k['high'] for k in w]; l=[k['low'] for k in w]; o=[k['open'] for k in w]
    tech=50; capital=50; info=50
    factors = []  # [{"dim":"技术面","name":"均线多头排列","delta":"+15","detail":"MA5>MA10>MA20"}]

    def add(dim, name, delta, detail=""):
        """记录一个加减分因子"""
        factors.append({"dim":dim,"name":name,"delta":delta,"detail":detail})

    # ---- 技术面 ----
    ma5,ma10,ma20=calc_ma(c,5),calc_ma(c,10),calc_ma(c,20)
    rsi=calc_rsi(c)
    if ma5 and ma10 and ma20:
        if ma5>ma10>ma20:
            tech+=15
            add("技术面","均线多头排列",15,f"MA5({ma5:.2f})>MA10({ma10:.2f})>MA20({ma20:.2f})")
        elif ma5<ma10<ma20:
            tech-=10
            add("技术面","均线空头排列",-10,f"MA5({ma5:.2f})<MA10({ma10:.2f})<MA20({ma20:.2f})")
    dif=calc_ema(c,12); dea=calc_ema(c,26)
    if dif and dea and dif>dea and dif>0:
        tech+=5
        add("技术面","MACD金叉且多头",5,f"DIF({dif:.2f})>DEA({dea:.2f}),DIF>0")
    elif dif and dif<0:
        tech-=3
        add("技术面","MACD空头区域",-3,f"DIF({dif:.2f})<0")
    if 40<=rsi<=55:
        tech+=8
        add("技术面","RSI健康回调区",8,f"RSI={rsi:.1f}(40-55)")
    elif 55<rsi<=70:
        tech+=12
        add("技术面","RSI强势区",12,f"RSI={rsi:.1f}(55-70)")
    elif rsi>75:
        tech+=5
        add("技术面","RSI超买区",5,f"RSI={rsi:.1f}(>75)")
    elif rsi<30:
        tech-=8
        add("技术面","RSI超卖区",-8,f"RSI={rsi:.1f}(<30)")
    av5=sum(v[-6:-1])/5 if len(v)>=6 else v[-1]
    vr5=v[-1]/av5 if av5>0 else 1
    if c[-1]>c[-2] and vr5>1.5:
        tech+=8
        add("技术面","放量上涨",8,f"收盘>前收且量比5日={vr5:.2f}")
    elif c[-1]<c[-2]:
        # V3回测(120天): 缩量下跌=陷阱, 适度放量回调=洗盘, 巨量下跌=出货
        if vr5<0.7:
            tech-=10
            add("技术面","缩量下跌(陷阱)",-10,f"收盘<前收,量比5日={vr5:.2f}<0.7,回测显示跑输基线")
        elif 1.1<=vr5<=1.5:
            tech+=5
            add("技术面","放量回调(洗盘)",5,f"收盘<前收,量比5日={vr5:.2f},回测显示20日超额+5.8%")
        elif vr5>1.5:
            tech-=8
            add("技术面","巨量下跌(出货)",-8,f"收盘<前收且量比5日={vr5:.2f}>1.5")
    if ma20:
        dev=(c[-1]-ma20)/ma20*100
        if 2<dev<8:
            tech+=8
            add("技术面","适度偏离MA20",8,f"偏离MA20 +{dev:.1f}%")
        elif dev>15:
            tech-=8
            add("技术面","过度偏离MA20",-8,f"偏离MA20 +{dev:.1f}%")
        elif -5<dev<-2:
            tech+=5
            add("技术面","回调至MA20附近",5,f"偏离MA20 {dev:.1f}%")
    if len(c)>=120:
        h52,l52=max(h[-120:]),min(l[-120:])
        pct52=(c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50
        if pct52<30:
            tech+=5
            add("技术面","52周低位区",5,f"52周位置{pct52:.0f}%")
        elif pct52>90:
            tech-=5
            add("技术面","52周高位区",-5,f"52周位置{pct52:.0f}%")
    if rsi>80:
        tech=min(tech,70)
        add("技术面","RSI严重超买封顶",0,f"RSI={rsi:.1f}>80,技术面封顶70")
    if ma5 and (c[-1]-ma5)/ma5*100>8:
        tech=min(tech,65)
        add("技术面","短期超涨封顶",0,f"偏离MA5 +{(c[-1]-ma5)/ma5*100:.1f}%>8%,技术面封顶65")
    tech=max(5,min(95,int(tech)))

    # ---- 资金面 ----
    tp=[(hi+lo+cl)/3 for hi,lo,cl in zip(h,l,c)]
    pf=nf=0
    for i in range(-14,0):
        mf=tp[i]*v[i]
        if tp[i]>tp[i-1]: pf+=mf
        else: nf+=mf
    mfi=100-100/(1+pf/nf) if nf>0 else 50
    if mfi>60:
        capital+=12
        add("资金面","MFI资金流入",12,f"MFI={mfi:.1f}(>60)")
    elif mfi<40:
        capital-=5
        add("资金面","MFI资金流出",-5,f"MFI={mfi:.1f}(<40)")
    mf_vol=[]
    for i in range(-20,0):
        if h[i]==l[i]: mf=0
        else: mf=((c[i]-l[i])-(h[i]-c[i]))/(h[i]-l[i])
        mf_vol.append(mf*v[i])
    cmf=sum(mf_vol)/sum(v[-20:]) if sum(v[-20:])>0 else 0
    if cmf>0.15:
        capital+=12
        add("资金面","CMF资金净流入",12,f"CMF={cmf:.2f}(>0.15)")
    elif cmf<-0.15:
        capital-=10
        add("资金面","CMF资金净流出",-10,f"CMF={cmf:.2f}(<-0.15)")
    tv=sum(v[-20:])
    if tv>0: vwap20=sum(c[i]*v[i] for i in range(-20,0))/tv
    else: vwap20=c[-1]
    if c[-1]>vwap20*1.03:
        capital+=5
        add("资金面","突破VWAP20",5,f"收盘{c[-1]:.2f}>VWAP20 {vwap20:.2f}×1.03")
    if len(v)>=5:
        vt=sum(1 for i in range(-5,0) if v[i]>v[i-1])
        if vt>=4:
            capital+=10
            add("资金面","持续放量",10,f"近5日中{vt}天放量")
    if h[-1]>l[-1]:
        cs=(c[-1]-l[-1])/(h[-1]-l[-1])*100
        if cs>80 and v[-1]>av5*1.3:
            capital+=10
            add("资金面","收盘强势+放量",10,f"收盘位置{cs:.0f}%且量>5日均量1.3倍")
        elif cs<20 and v[-1]>av5*1.3:
            capital-=10
            add("资金面","收盘弱势+放量",-10,f"收盘位置{cs:.0f}%且量>5日均量1.3倍")
    capital=max(5,min(95,int(capital)))

    # ---- 信息面 ----
    r3=(c[-1]/c[-4]-1)*100 if len(c)>=4 else 0
    if r3>8:
        info+=15
        add("信息面","3日强势上涨",15,f"3日涨幅+{r3:.1f}%")
    elif r3<-8:
        info-=12
        add("信息面","3日大幅下跌",-12,f"3日涨幅{r3:.1f}%")
    gap=(o[-1]-c[-2])/c[-2]*100 if len(c)>=2 else 0
    if abs(gap)>3:
        if gap>0:
            info+=10
            add("信息面","跳空高开",10,f"缺口+{gap:.1f}%")
        else:
            info-=5
            add("信息面","跳空低开",-5,f"缺口{gap:.1f}%")
    av20=sum(v[-21:-1])/20 if len(v)>=21 else av5
    vs=v[-1]/av20 if av20>0 else 1
    if vs>3:
        info+=10
        add("信息面","巨量比",10,f"量比={vs:.1f}(>3)")
    elif vs>2:
        info+=5
        add("信息面","大量比",5,f"量比={vs:.1f}(>2)")
    if len(c)>=3 and c[-1]>c[-2]>c[-3]:
        info+=8
        add("信息面","三连涨",8,f"连涨3日")
    if len(c)>=3 and c[-1]<c[-2] and c[-2]<c[-3]:
        info+=5
        add("信息面","连跌2日(反弹信号)",5,"连跌2日,V3回测:高评分连跌后20日超额+1.6%")
    if gap>2 and c[-1]<o[-1]:
        info=min(info,60)
        add("信息面","冲高回落封顶",0,f"跳空+{gap:.1f}%但收阴,信息面封顶60")
    info=max(5,min(95,int(info)))

    return {"score":round(tech*0.4+capital*0.4+info*0.2),"tech":tech,"capital":capital,"info":info,"factors":factors}

def get_suggestion(score):
    if score>=75: return "🔥强烈买入","strong_buy"
    elif score>=70: return "🟢逢低买入","buy"
    elif score>=60: return "🟡持有","hold"
    elif score>=45: return "⚪观望","watch"
    else: return "🔴回避","avoid"

# ========== 主题板块（12个以内，合并近似概念）==========
THEME_SECTORS = {
    "半导体": ["半导体", "PCB", "存储芯片", "AI芯片", "半导体材料", "半导体设备", "半导体检测", "半导体/MLCC", "半导体/封测", "半导体/CPU", "半导体/硅片", "半导体/晶振", "半导体/存储", "半导体/功率", "半导体/代工", "半导体/接口", "半导体/特气", "半导体材料/光刻胶", "半导体材料/锗"],
    "光通信/CPO": ["光通信", "CPO", "光模块", "光通信/CPO", "光通信/光纤", "光通信/激光", "光通信/激光器", "光通信/滤光片", "光通信/晶体", "光通信/光学"],
    "AI/算力": ["AI", "算力", "AI服务器", "AI应用", "AI芯片", "机器视觉", "数字经济", "云计算", "IDC", "智能硬件", "安防/AI", "AI应用/教育", "AI应用/软件", "AI应用/传媒", "机器视觉/AI", "算力/AI服务器", "算力/IDC", "算力/通信", "云计算/IDC"],
    "新能源/储能": ["新能源", "储能", "光伏", "锂电", "风电", "电力", "电力设备", "液冷", "新能源/锂电池", "新能源/氟化工", "新能源/汽车", "光伏/储能", "光伏/自动化", "液冷/储能", "风电/锻造", "电力设备/变压器", "电力设备/特高压", "电力设备/电缆", "电力设备/照明", "煤炭/能源"],
    "机器人/智能制造": ["机器人", "智能制造", "自动化", "工业自动化", "机器人/减速器", "机器人/电机", "机器人/轴承", "机器人/热管理", "机器人/精密件"],
    "汽车零部件": ["汽车", "零部件", "制动", "热管理", "传感器", "精密件", "汽车零部件/热管理", "汽车零部件/传感器", "汽车零部件/精密件", "汽车零部件/制动"],
    "消费电子": ["消费电子", "电子", "FPC", "连接器", "玻璃", "TV", "结构件", "消费电子/连接器", "消费电子/FPC", "消费电子/玻璃", "消费电子/TV", "消费电子/结构件"],
    "新材料": ["新材料", "化工", "玻纤", "陶瓷", "绝缘", "膜", "钛合金", "锆", "硅化工", "复材", "新材料/硅化工", "新材料/锆", "新材料/复材", "新材料/陶瓷", "新材料/玻纤", "新材料/绝缘", "新材料/化工", "新材料/膜", "新材料/钛合金"],
    "游戏/传媒": ["游戏", "传媒", "影视", "营销", "出版", "广告", "数据", "游戏/AI", "游戏/出海", "影视/传媒", "传媒/营销", "传媒/出版", "传媒/数据", "传媒/广告"],
    "金融/交通": ["金融", "银行", "保险", "证券", "交通运输", "铁路", "金融/银行", "交通运输/铁路"],
    "医疗/其他": ["医疗", "医药", "ST", "水利", "泵阀", "航空维修", "家居", "医疗信息化", "家居/ST", "泵阀/水利", "航空维修"],
}

# ========== 细粒度板块（每只股票的精确板块）==========
STOCK_SECTOR = {
    "000034":"数字经济","000559":"汽车零部件","000636":"半导体/MLCC","000791":"电力","000977":"AI服务器",
    "001309":"存储芯片","001339":"智能硬件","002009":"智能制造","002027":"传媒/广告","002028":"电力设备",
    "002050":"机器人/热管理","002112":"电力设备","002130":"消费电子","002131":"泵阀/水利","002149":"新材料/钛合金",
    "002222":"光通信/晶体","002261":"AI应用/教育","002272":"液冷/储能","002281":"光通信","002284":"汽车零部件/制动",
    "002338":"半导体设备","002371":"半导体设备","002384":"消费电子/FPC","002407":"新能源/氟化工","002415":"安防/AI",
    "002428":"半导体材料/锗","002429":"消费电子/TV","002463":"PCB/半导体","002472":"机器人/减速器","002475":"消费电子/连接器",
    "002517":"游戏/AI","002536":"汽车零部件/热管理","002555":"游戏/出海","002602":"游戏/AI","002837":"液冷/储能",
    "002896":"机器人/减速器","002916":"PCB/半导体","002927":"电力设备","300014":"新能源/锂电池","300054":"半导体材料",
    "300058":"传媒/营销","300093":"光伏/储能","300096":"医疗信息化","300124":"工业自动化","300182":"影视/传媒",
    "300274":"光伏/储能","300285":"新材料/陶瓷","300291":"影视/传媒","300308":"光通信/CPO","300346":"半导体材料",
    "300394":"光通信/CPO","300408":"半导体/MLCC","300418":"AI应用","300424":"航空维修","300433":"消费电子/玻璃",
    "300442":"算力/IDC","300476":"PCB/半导体","300481":"新材料/化工","300499":"液冷/储能","300502":"光通信/CPO",
    "300533":"游戏/出海","300567":"半导体检测","300570":"光通信/CPO","300580":"机器人/精密件","300624":"AI应用/软件",
    "300643":"汽车零部件/传感器","300655":"半导体材料/光刻胶","300666":"半导体材料","300738":"算力/IDC","300757":"光伏/自动化",
    "300806":"新材料/膜","300827":"光伏/储能","301012":"电力设备/变压器","301183":"光通信/滤光片","301231":"AI应用/传媒",
    "301308":"存储芯片","301358":"新能源/锂电池","301486":"消费电子/连接器","301526":"新材料/复材","301630":"新能源/汽车",
    "600021":"电力","600089":"电力设备/特高压","600176":"新材料/玻纤","600550":"电力设备/变压器","600584":"半导体/封测",
    "600633":"传媒/数据","600919":"金融/银行","601006":"交通运输/铁路","601208":"新材料/绝缘","601225":"煤炭/能源",
    "601328":"金融/银行","601398":"金融/银行","601689":"汽车零部件/热管理","601869":"光通信/光纤","601921":"传媒/出版",
    "603019":"算力/AI服务器","603040":"汽车零部件/精密件","603083":"光通信/CPO","603119":"汽车零部件","603220":"算力/通信",
    "603228":"PCB/半导体","603389":"家居/ST","603444":"游戏","603618":"电力设备/电缆","603626":"消费电子/结构件",
    "603663":"新材料/锆","603667":"机器人/轴承","603685":"电力设备/照明","603728":"机器人/电机","603738":"半导体/晶振",
    "603938":"新材料/硅化工","603985":"风电/锻造","603986":"半导体/存储","688008":"半导体/接口","688012":"半导体设备",
    "688017":"机器人/减速器","688025":"光通信/激光","688041":"半导体/CPU","688126":"半导体/硅片","688146":"半导体/特气",
    "688158":"云计算/IDC","688160":"机器人/电机","688167":"光通信/激光","688183":"PCB/半导体","688195":"光通信/光学",
    "688256":"AI芯片","688268":"半导体/特气","688347":"半导体/代工","688396":"半导体/功率","688400":"机器视觉/AI",
    "688498":"光通信/激光器","688515":"半导体/接口","688676":"电力设备/变压器",
}

def get_theme(code):
    """细粒度板块 → 主题板块（12个以内）"""
    s = STOCK_SECTOR.get(code, "其他")
    for theme, keywords in THEME_SECTORS.items():
        for kw in keywords:
            if kw in s:
                return theme
    return "其他"

# ========== 生成报告 ==========
def generate_report(results, today, output_dir, hist_scores=None):
    if hist_scores is None: hist_scores = {}
    
    def trend_str(code):
        """过去5个交易日的评分趋势，如 68→72→75→73→79 ↑"""
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return "-"
        # 取最近5天的评分
        recent = [int(s) for s in scores[-5:]]
        # 去重：连续的相同分数合并为一个
        deduped = []
        for s in recent:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        if len(deduped) < 2: return str(deduped[0]) if deduped else "-"
        # 箭头方向
        if deduped[-1] > deduped[-2]: arrow = "↑"
        elif deduped[-1] < deduped[-2]: arrow = "↓"
        else: arrow = "→"
        return "→".join(str(s) for s in deduped) + f" {arrow}"
    
    strong_buy=[r for r in results if r["sug_action"]=="strong_buy"]
    buy_list=[r for r in results if r["sug_action"]=="buy"]
    hold_list=[r for r in results if r["sug_action"]=="hold"]
    watch_list=[r for r in results if r["sug_action"]=="watch"]
    avoid_list=[r for r in results if r["sug_action"]=="avoid"]
    risky=[r for r in avoid_list if r["score"]<45]
    critical=[r for r in avoid_list if r["score"]<40]
    
    sectors={}
    for r in results:
        s=r.get("sector","其他"); sectors[s]=sectors.get(s,0)+1
    top_sectors=sorted(sectors.items(),key=lambda x:x[1],reverse=True)[:8]
    
    sector_groups={}
    for r in results:
        sec=r.get("sector","其他")
        if sec not in sector_groups: sector_groups[sec]=[]
        sector_groups[sec].append(r)
    sorted_sectors=sorted(sector_groups.items(),key=lambda x:max(r["score"] for r in x[1]),reverse=True)
    
    # 表格列（含收盘价）
    hdr="| 代码 | 名称 | 概念板块 | SS分 | 5日评分趋势 | 收盘价 | 日涨跌 | 5日涨跌 | 20日涨跌 | 建议 |"
    sep="|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"
    rhdr="| 代码 | 名称 | 概念板块 | SS分 | 5日评分趋势 | 收盘价 | 日涨跌 | 5日涨跌 | 20日涨跌 | 风险 |"
    rsep="|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"
    sec_hdr="| 代码 | 名称 | SS分 | 5日评分趋势 | 收盘价 | 日涨跌 | 5日涨跌 | 20日涨跌 | 建议 |"
    sec_sep="|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"
    
    def row(r, cls=""):
        t=trend_str(r["code"])
        return f"| {r['code']} | {r['name']} | {r.get('sector','')} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {r['suggestion']} |"
    
    md=[]
    md.append(f"# 📊 SS-Enhanced 股票评分日报")
    md.append(f"**日期**: {today} | **模型**: SS-Enhanced V2 | **股票池**: {len(results)}只A股")
    md.append(f"")
    md.append(f"## 📈 市场概况")
    md.append(f"- 🔥强烈买入(≥75): **{len(strong_buy)}只** | 🟢逢低买入(≥70): **{len(buy_list)}只** | 🟡持有(60-69): **{len(hold_list)}只** | ⚪观望(45-59): **{len(watch_list)}只** | 🔴回避(<45): **{len(avoid_list)}只**")
    md.append(f"- 🚨触发卖出(<40): **{len(critical)}只** | ⚠️持续低分预警: **{len(risky)}只**")
    md.append(f"- 板块覆盖: {len(sectors)}个 | 主力: {', '.join(f'{s}({n})' for s,n in top_sectors)}")
    md.append(f"")
    
    # ===== 第一部分：TOP 5 + 最差 5 =====
    md.append(f"## 🏆 综合排名概览")
    md.append(f"")
    md.append(f"### 🔥 最佳 TOP 5")
    md.append(f"")
    md.append(hdr); md.append(sep)
    for r in results[:5]:
        md.append(row(r))
    md.append("")
    
    md.append(f"### 🔴 最差 BOTTOM 5")
    md.append(f"")
    if critical:
        md.append(f"> 🚨 其中 **{len([r for r in results[-5:] if r['score']<40])}只** 已触发卖出阈值")
        md.append(f"")
    md.append(rhdr); md.append(rsep)
    for r in results[-5:]:
        risk = "🚨强制卖出" if r["score"] < 40 else "⚠️持续低分"
        t=trend_str(r["code"])
        md.append(f"| {r['code']} | {r['name']} | {r.get('sector','')} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {risk} |")
    md.append("")
    
    # ===== 第二部分：全部股票按概念板块分组 =====
    md.append(f"## 📊 按概念板块排名（共{len(sorted_sectors)}个板块）")
    md.append(f"")
    for sec_name, sec_stocks in sorted_sectors:
        sec_best = max(r["score"] for r in sec_stocks)
        sec_worst = min(r["score"] for r in sec_stocks)
        sec_buy = sum(1 for r in sec_stocks if r["sug_action"] in ("strong_buy","buy"))
        sec_avoid = sum(1 for r in sec_stocks if r["sug_action"]=="avoid")
        flags = []
        if sec_buy > 0: flags.append(f"🟢推荐{sec_buy}只")
        if sec_avoid > 0: flags.append(f"🔴回避{sec_avoid}只")
        flag_str = " | ".join(flags)
        md.append(f"### {sec_name} ({len(sec_stocks)}只, 最高{sec_best}/最低{sec_worst}) {flag_str}")
        md.append(f"")
        md.append(sec_hdr); md.append(sec_sep)
        for r in sorted(sec_stocks, key=lambda x: x["score"], reverse=True):
            t=trend_str(r["code"])
            md.append(f"| {r['code']} | {r['name']} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {r['suggestion']} |")
        md.append("")
    
    md_path=os.path.join(output_dir,f"SS增强版评分_{today}.md")
    with open(md_path,"w") as f: f.write("\n".join(md))
    
    # HTML 交互版（Tab切换，用于浏览器/聊天窗口预览）
    html = generate_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors)
    html_path=os.path.join(output_dir,f"SS增强版评分_{today}.html")
    with open(html_path,"w") as f: f.write(html)
    
    # HTML 邮件版（全内联样式，无JS，分段表格）
    email_html = generate_email_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors)
    email_html_path=os.path.join(output_dir,f"SS增强版评分_{today}_email.html")
    with open(email_html_path,"w") as f: f.write(email_html)
    
    return md_path, html_path, email_html_path


def generate_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors):
    """生成带Tab切换的HTML报告（斑马纹、固定列宽、板块分sheet）"""

    def trend_str(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return "-"
        recent = [int(s) for s in scores[-5:]]
        deduped = []
        for s in recent:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        if len(deduped) < 2: return str(deduped[0]) if deduped else "-"
        if deduped[-1] > deduped[-2]: arrow = "↑"
        elif deduped[-1] < deduped[-2]: arrow = "↓"
        else: arrow = "→"
        return "→".join(str(s) for s in deduped) + f" {arrow}"

    def score_badge(score):
        """返回 (badge_html, suggestion_color)"""
        if score >= 75:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fef3c7;color:#92400e;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#92400e"
        elif score >= 70:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dcfce7;color:#166534;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#166534"
        elif score >= 60:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dbeafe;color:#1e40af;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#1e40af"
        elif score >= 45:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#f1f5f9;color:#475569;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#475569"
        else:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fee2e2;color:#991b1b;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#991b1b"

    def trend_color(t):
        if t.endswith("↑"): return "#16a34a"
        elif t.endswith("↓"): return "#dc2626"
        elif t.endswith("→"): return "#94a3b8"
        return "#333"

    def pct_color(val):
        if val > 0: return "color:#dc2626"
        elif val < 0: return "color:#16a34a"
        return "color:#666"

    # ---- 样式常量 ----
    TH_BASE = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;position:sticky;top:0;z-index:1"
    TD_BASE = 'padding:7px 6px;font-size:13px;color:#333;white-space:nowrap'
    TD_NUM  = 'padding:7px 6px;font-size:13px;color:#333;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums'

    def make_factors_detail_html(r):
        """生成加减分因子明细HTML（用于展开行）"""
        factors = r.get("factors", [])
        if not factors:
            return '<p style="color:#999;font-size:12px;margin:0">暂无分析数据</p>'
        pos = sorted([f for f in factors if f["delta"] > 0], key=lambda x: x["delta"], reverse=True)
        neg = sorted([f for f in factors if f["delta"] < 0], key=lambda x: x["delta"])
        caps = [f for f in factors if f["delta"] == 0]
        parts = []
        # 维度分值
        parts.append(f'<div style="margin-bottom:8px"><span style="font-size:12px;color:#666;margin-right:12px">技术面 <b style="color:#1a1a2e;font-size:14px">{r.get("tech",50)}</b></span><span style="font-size:12px;color:#666;margin-right:12px">资金面 <b style="color:#1a1a2e;font-size:14px">{r.get("capital",50)}</b></span><span style="font-size:12px;color:#666">信息面 <b style="color:#1a1a2e;font-size:14px">{r.get("info",50)}</b></span><span style="font-size:12px;color:#888;margin-left:12px">权重: 技术40% · 资金40% · 信息20%</span></div>')
        if pos:
            parts.append('<div style="margin-bottom:6px"><span style="font-size:12px;color:#166534;font-weight:600">🟢 加分项</span></div>')
            tags = []
            for f in pos:
                tags.append(f'<span style="display:inline-block;margin:2px 4px;padding:3px 10px;border-radius:6px;background:#dcfce7;color:#166534;font-size:12px">{f["name"]} <b>+{f["delta"]}</b><span style="color:#888;font-size:11px;margin-left:4px">{f["detail"]}</span></span>')
            parts.append(f'<div style="margin-bottom:8px">{"".join(tags)}</div>')
        if neg:
            parts.append('<div style="margin-bottom:6px"><span style="font-size:12px;color:#991b1b;font-weight:600">🔴 减分项</span></div>')
            tags = []
            for f in neg:
                tags.append(f'<span style="display:inline-block;margin:2px 4px;padding:3px 10px;border-radius:6px;background:#fee2e2;color:#991b1b;font-size:12px">{f["name"]} <b>{f["delta"]}</b><span style="color:#888;font-size:11px;margin-left:4px">{f["detail"]}</span></span>')
            parts.append(f'<div style="margin-bottom:8px">{"".join(tags)}</div>')
        if caps:
            parts.append('<div style="margin-bottom:6px"><span style="font-size:12px;color:#856404;font-weight:600">⚠️ 封顶/限制项</span></div>')
            tags = []
            for f in caps:
                tags.append(f'<span style="display:inline-block;margin:2px 4px;padding:3px 10px;border-radius:6px;background:#fff3cd;color:#856404;font-size:12px">{f["name"]}<span style="color:#888;font-size:11px;margin-left:4px">{f["detail"]}</span></span>')
            parts.append(f'<div>{"".join(tags)}</div>')
        return "".join(parts)

    def make_row_html(r, row_idx):
        """生成一行HTML（主行+展开分析行），含斑马纹"""
        t = trend_str(r["code"])
        tc = trend_color(t)
        badge, sug_color = score_badge(r["score"])
        bg = "#f8f9fc" if row_idx % 2 == 0 else "#ffffff"
        price = f'{r["price"]:.2f}'
        cp = pct_color(r["change_pct"])
        r5 = pct_color(r["ret_5d"])
        r20 = pct_color(r["ret_20d"])
        detail_id = f"detail-{r['code']}-{row_idx}"
        main_row = f"""<tr style="background:{bg};cursor:pointer" onclick="toggleDetail('{detail_id}')">
<td style="{TD_BASE};text-align:center;font-size:14px">📊</td>
<td style="{TD_BASE}">{r['code']}</td>
<td style="{TD_BASE};font-weight:500">{r['name']}</td>
<td style="{TD_BASE}">{r.get('sector','')}</td>
<td style="{TD_BASE};text-align:center">{badge}</td>
<td style="{TD_BASE};color:{tc};font-size:12px">{t}</td>
<td style="{TD_NUM}">{price}</td>
<td style="{TD_NUM};{cp}">{r['change_pct']:+.2f}%</td>
<td style="{TD_NUM};{r5}">{r['ret_5d']:+.1f}%</td>
<td style="{TD_NUM};{r20}">{r['ret_20d']:+.1f}%</td>
<td style="{TD_BASE};color:{sug_color};font-weight:600;font-size:12px">{r['suggestion']}</td>
</tr>"""
        detail_row = f"""<tr id="{detail_id}" style="display:none">
<td colspan="11" style="padding:12px 16px;background:#f8f9fc;border-bottom:2px solid #e8ecf1">
{make_factors_detail_html(r)}
</td></tr>"""
        return main_row + "\n" + detail_row

    def make_table_html(stocks):
        """生成一个完整表格的 tbody HTML"""
        sorted_stocks = sorted(stocks, key=lambda x: x["score"], reverse=True)
        rows = []
        for i, r in enumerate(sorted_stocks):
            rows.append(make_row_html(r, i))
        return "\n".join(rows)

    # ---- 按股票数量降序排列板块 ----
    sectors_by_count = sorted(sorted_sectors, key=lambda x: len(x[1]), reverse=True)

    # ---- 构建Tab列表：全部 + 各板块 ----
    tabs = []
    # Tab 0: 全部
    tabs.append(("全部", len(results), make_table_html(results)))
    # Tab 1..N: 各板块（按数量降序）
    for sec_name, sec_stocks in sectors_by_count:
        tabs.append((sec_name, len(sec_stocks), make_table_html(sec_stocks)))

    # ---- 生成Tab按钮HTML ----
    tab_buttons = []
    for i, (name, count, _) in enumerate(tabs):
        active = "tab-active" if i == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn {active}" onclick="switchTab({i})">'
            f'{name} <span class="tab-count">{count}</span></button>'
        )
    tab_buttons_html = "\n".join(tab_buttons)

    # ---- 生成各Tab内容面板HTML ----
    tab_panels = []
    colgroup = """<colgroup>
<col style="width:32px"><col style="width:62px"><col style="width:72px"><col style="width:110px"><col style="width:52px">
<col style="width:150px"><col style="width:64px"><col style="width:60px"><col style="width:56px"><col style="width:56px"><col style="width:80px">
</colgroup>"""
    thead = f"""<thead><tr>
<th style="{TH_BASE};text-align:center">分析</th>
<th style="{TH_BASE};text-align:center">代码</th>
<th style="{TH_BASE}">名称</th>
<th style="{TH_BASE}">概念板块</th>
<th style="{TH_BASE};text-align:center">SS分</th>
<th style="{TH_BASE}">5日评分趋势</th>
<th style="{TH_BASE};text-align:right">收盘价</th>
<th style="{TH_BASE};text-align:right">日涨跌</th>
<th style="{TH_BASE};text-align:right">5日涨跌</th>
<th style="{TH_BASE};text-align:right">20日涨跌</th>
<th style="{TH_BASE}">建议</th>
</tr></thead>"""

    for i, (name, count, tbody_html) in enumerate(tabs):
        display = "block" if i == 0 else "none"
        sec_best = max(r["score"] for r in results if r.get("sector") == name) if name != "全部" else max(r["score"] for r in results)
        sec_worst = min(r["score"] for r in results if r.get("sector") == name) if name != "全部" else min(r["score"] for r in results)
        sec_buy = sum(1 for r in results if r.get("sector") == name and r["sug_action"] in ("strong_buy","buy")) if name != "全部" else len(strong_buy)+len(buy_list)
        sec_avoid = sum(1 for r in results if r.get("sector") == name and r["sug_action"]=="avoid") if name != "全部" else len(avoid_list)

        tags = ""
        if sec_buy > 0:
            tags += f'<span class="sec-tag" style="background:#dcfce7;color:#166534">🟢 推荐{sec_buy}</span>'
        if sec_avoid > 0:
            tags += f'<span class="sec-tag" style="background:#fee2e2;color:#991b1b">🔴 回避{sec_avoid}</span>'

        tab_panels.append(f"""<div id="tab-panel-{i}" class="tab-panel" style="display:{display}">
<div class="sec-info"><span class="sec-name">📌 {name}</span><span class="sec-meta">{count}只 · 最高{sec_best}/最低{sec_worst}</span>{tags}</div>
<div class="table-wrap">
<table style="width:100%;border-collapse:collapse;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;table-layout:fixed">
{colgroup}{thead}<tbody>{tbody_html}</tbody></table></div></div>""")
    tab_panels_html = "\n".join(tab_panels)

    # ---- 统计卡片 ----
    stat_cards = [
        ("#fbbf24", "#fef3c7", "#92400e", len(strong_buy), "🔥 强烈买入 ≥75"),
        ("#4ade80", "#dcfce7", "#166534", len(buy_list), "🟢 逢低买入 ≥70"),
        ("#60a5fa", "#dbeafe", "#1e40af", len(hold_list), "🟡 持有 60-69"),
        ("#94a3b8", "#f1f5f9", "#475569", len(watch_list), "⚪ 观望 45-59"),
        ("#f87171", "#fee2e2", "#991b1b", len(avoid_list), "🔴 回避 <45"),
        ("#ef4444", "#fecaca", "#7f1d1d", len(critical), "🚨 触发卖出 <40"),
    ]
    card_html = ""
    for color, bg, text_color, count, label in stat_cards:
        card_html += f'<div style="background:{bg};border-radius:10px;padding:12px 8px;text-align:center;border:1px solid {color}33;flex:1;min-width:100px"><div style="font-size:24px;font-weight:800;color:{text_color};line-height:1.2">{count}</div><div style="font-size:11px;color:{text_color};margin-top:3px">{label}</div></div>'

    # ---- TOP5 快速摘要 ----
    top5_summary = ""
    for r in results[:5]:
        top5_summary += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#fef3c7;font-size:12px;color:#92400e;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b></span>'

    bottom5_summary = ""
    for r in results[-5:]:
        bg_color = "#fecaca" if r["score"] < 40 else "#fee2e2"
        text_color = "#7f1d1d" if r["score"] < 40 else "#991b1b"
        bottom5_summary += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:{bg_color};font-size:12px;color:{text_color};font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b></span>'

    # ---- 完整HTML ----
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS-Enhanced 股票评分日报 {today}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:0; background:#eef0f5; font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; }}
.container {{ max-width:900px; margin:0 auto; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 2px 16px rgba(0,0,0,0.08); }}
.header {{ padding:24px 28px 14px; background:linear-gradient(135deg,#1a1a2e,#16213e); }}
.header h1 {{ margin:0 0 6px; font-size:20px; color:#e94560; }}
.header p {{ margin:0; font-size:13px; color:#94a3b8; }}
.header strong {{ color:#e6edf3; }}
.stats {{ display:flex; gap:8px; padding:16px 28px 8px; flex-wrap:wrap; }}
.stat-note {{ padding:0 28px 8px; font-size:12px; color:#888; }}
.summary {{ padding:8px 28px; }}
.summary h3 {{ margin:8px 0 6px; font-size:14px; }}
.top5 {{ margin-bottom:12px; }}
.bottom5 {{}}
.tab-bar {{ display:flex; gap:4px; padding:8px 28px 0; flex-wrap:wrap; border-bottom:2px solid #e8ecf1; }}
.tab-btn {{ padding:7px 14px; border:none; border-radius:8px 8px 0 0; cursor:pointer; font-size:13px; font-weight:500; background:#f1f5f9; color:#64748b; transition:all .15s; font-family:inherit; }}
.tab-btn:hover {{ background:#e2e8f0; color:#334155; }}
.tab-btn.tab-active {{ background:#1a1a2e; color:#fff; font-weight:600; }}
.tab-btn .tab-count {{ display:inline-block; margin-left:3px; padding:1px 6px; border-radius:8px; background:rgba(0,0,0,0.12); font-size:11px; }}
.tab-btn.tab-active .tab-count {{ background:rgba(255,255,255,0.2); }}
.tab-panel {{ padding:12px 28px 20px; }}
.sec-info {{ margin-bottom:8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.sec-name {{ font-size:14px; font-weight:600; color:#1a1a2e; }}
.sec-meta {{ font-size:12px; color:#888; }}
.sec-tag {{ display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; font-weight:500; }}
.table-wrap {{ border:1px solid #e2e8f0; border-radius:8px; overflow:auto; max-height:600px; }}
.table-wrap table {{ width:100%; border-collapse:collapse; }}
.table-wrap thead tr {{ position:sticky; top:0; z-index:1; }}
.table-wrap tbody tr:hover {{ background:#eef2ff !important; }}
.footer {{ padding:14px 28px 20px; text-align:center; border-top:1px solid #e8ecf1; }}
.footer p {{ margin:0; font-size:11px; color:#999; }}
</style>
<script>
function switchTab(idx) {{
  document.querySelectorAll('.tab-panel').forEach((p,i)=>{{ p.style.display = i===idx ? 'block' : 'none'; }});
  document.querySelectorAll('.tab-btn').forEach((b,i)=>{{ b.className = i===idx ? 'tab-btn tab-active' : 'tab-btn'; }});
}}
function toggleDetail(id) {{
  var el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}}
</script>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📊 SS-Enhanced 股票评分日报</h1>
    <p><strong>{today}</strong> &nbsp;|&nbsp; SS-Enhanced V2 &nbsp;|&nbsp; {len(results)}只A股 &nbsp;|&nbsp; 板块覆盖: {len(sectors)}个</p>
  </div>
  <div class="stats">{card_html}</div>
  <p class="stat-note">主力板块: {', '.join(f'{s}({n})' for s, n in top_sectors)}</p>
  <div class="summary">
    <div class="top5"><h3 style="color:#92400e">🔥 最佳 TOP 5</h3>{top5_summary}</div>
    <div class="bottom5"><h3 style="color:#991b1b">🔴 最差 BOTTOM 5</h3>{bottom5_summary}</div>
  </div>
  <div class="tab-bar">{tab_buttons_html}</div>
  {tab_panels_html}
  <div class="footer">
    <p>自动生成 | SS-Enhanced V2 | 策略: ≥75强烈买入 · ≥70逢低买入 · ≥60持有 · ≥45观望 · &lt;45回避 · &lt;40触发卖出</p>
    <p style="margin-top:4px">V3回测(120天·15891条): 评分≥75纯高分10日超额+2.65% · 放量回调(1.1-1.5x)20日超额+5.8% · 缩量回调解读为陷阱</p>
  </div>
</div>
</body></html>"""

    return html


def generate_email_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors):
    """生成邮件兼容的HTML报告（全内联样式，无JS，分段表格，与交互版视觉一致）"""

    def trend_str(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return "-"
        recent = [int(s) for s in scores[-5:]]
        deduped = []
        for s in recent:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        if len(deduped) < 2: return str(deduped[0]) if deduped else "-"
        if deduped[-1] > deduped[-2]: arrow = "↑"
        elif deduped[-1] < deduped[-2]: arrow = "↓"
        else: arrow = "→"
        return "→".join(str(s) for s in deduped) + f" {arrow}"

    def score_badge(score):
        if score >= 75:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fef3c7;color:#92400e;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#92400e"
        elif score >= 70:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dcfce7;color:#166534;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#166534"
        elif score >= 60:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dbeafe;color:#1e40af;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#1e40af"
        elif score >= 45:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#f1f5f9;color:#475569;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#475569"
        else:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fee2e2;color:#991b1b;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#991b1b"

    def trend_color(t):
        if t.endswith("↑"): return "#16a34a"
        elif t.endswith("↓"): return "#dc2626"
        elif t.endswith("→"): return "#94a3b8"
        return "#333"

    def pct_style(val):
        if val > 0: return "color:#dc2626"
        elif val < 0: return "color:#16a34a"
        return "color:#666"

    TH = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;text-align:left"
    TH_R = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;text-align:right"
    TH_C = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;text-align:center"
    TD = 'padding:7px 6px;font-size:13px;color:#333;white-space:nowrap'
    TD_R = 'padding:7px 6px;font-size:13px;color:#333;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums'
    TD_C = 'padding:7px 6px;font-size:13px;color:#333;text-align:center;white-space:nowrap'
    TBL = "width:100%;border-collapse:collapse;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;table-layout:fixed"

    def make_factors_compact(r):
        """生成紧凑版加减分标签（用于邮件子行）"""
        factors = r.get("factors", [])
        if not factors:
            return ""
        pos = sorted([f for f in factors if f["delta"] > 0], key=lambda x: x["delta"], reverse=True)[:3]
        neg = sorted([f for f in factors if f["delta"] < 0], key=lambda x: x["delta"])[:2]
        tags = []
        for f in pos:
            tags.append(f'<span style="display:inline-block;margin:1px 3px;padding:1px 6px;border-radius:4px;background:#dcfce7;color:#166534;font-size:11px">{f["name"]}+{f["delta"]}</span>')
        for f in neg:
            tags.append(f'<span style="display:inline-block;margin:1px 3px;padding:1px 6px;border-radius:4px;background:#fee2e2;color:#991b1b;font-size:11px">{f["name"]}{f["delta"]}</span>')
        return "".join(tags)

    def make_row(r, row_idx, with_sector=True):
        t = trend_str(r["code"])
        tc = trend_color(t)
        badge, sug_color = score_badge(r["score"])
        bg = "#f8f9fc" if row_idx % 2 == 0 else "#ffffff"
        price = f'{r["price"]:.2f}'
        cp = pct_style(r["change_pct"])
        r5 = pct_style(r["ret_5d"])
        r20 = pct_style(r["ret_20d"])
        sector_cell = f'<td style="{TD}">{r.get("sector","")}</td>' if with_sector else ""
        colspan = "10" if with_sector else "9"
        factors_tags = make_factors_compact(r)
        factors_row = f"""<tr style="background:{bg}">
<td colspan="{colspan}" style="padding:2px 8px 6px;font-size:11px;color:#666;border-bottom:1px solid #e8ecf1">
<span style="color:#888;margin-right:4px">技术{r.get("tech",50)}/资金{r.get("capital",50)}/信息{r.get("info",50)}</span>{factors_tags}
</td></tr>""" if factors_tags else ""
        return f"""<tr style="background:{bg}">
<td style="{TD}">{r['code']}</td>
<td style="{TD};font-weight:500">{r['name']}</td>
{sector_cell}
<td style="{TD_C}">{badge}</td>
<td style="{TD};color:{tc};font-size:12px">{t}</td>
<td style="{TD_R}">{price}</td>
<td style="{TD_R};{cp}">{r['change_pct']:+.2f}%</td>
<td style="{TD_R};{r5}">{r['ret_5d']:+.1f}%</td>
<td style="{TD_R};{r20}">{r['ret_20d']:+.1f}%</td>
<td style="{TD};color:{sug_color};font-weight:600;font-size:12px">{r['suggestion']}</td>
</tr>{chr(10) + factors_row if factors_row else ''}"""

    def make_table(stocks, with_sector=True):
        sorted_stocks = sorted(stocks, key=lambda x: x["score"], reverse=True)
        rows = [make_row(r, i, with_sector) for i, r in enumerate(sorted_stocks)]
        sector_th = f'<th style="{TH}">概念板块</th>' if with_sector else ""
        col_w_sector = '<col style="width:110px">' if with_sector else ""
        col_w_others = 'width:72px' if with_sector else 'width:80px'
        return f"""<table style="{TBL}">
<colgroup>
<col style="width:62px"><col style="width:72px">{col_w_sector}<col style="width:52px">
<col style="width:150px"><col style="width:64px"><col style="width:60px"><col style="width:56px"><col style="width:56px"><col style="width:80px">
</colgroup>
<thead><tr>
<th style="{TH_C}">代码</th>
<th style="{TH}">名称</th>
{sector_th}
<th style="{TH_C}">SS分</th>
<th style="{TH}">5日评分趋势</th>
<th style="{TH_R}">收盘价</th>
<th style="{TH_R}">日涨跌</th>
<th style="{TH_R}">5日涨跌</th>
<th style="{TH_R}">20日涨跌</th>
<th style="{TH}">建议</th>
</tr></thead>
<tbody>{chr(10).join(rows)}</tbody>
</table>"""

    # ---- 按股票数量降序排列板块 ----
    sectors_by_count = sorted(sorted_sectors, key=lambda x: len(x[1]), reverse=True)

    # ---- 统计卡片 ----
    stat_cards = [
        ("#fef3c7", "#92400e", len(strong_buy), "🔥 强烈买入 ≥75"),
        ("#dcfce7", "#166534", len(buy_list), "🟢 逢低买入 ≥70"),
        ("#dbeafe", "#1e40af", len(hold_list), "🟡 持有 60-69"),
        ("#f1f5f9", "#475569", len(watch_list), "⚪ 观望 45-59"),
        ("#fee2e2", "#991b1b", len(avoid_list), "🔴 回避 <45"),
        ("#fecaca", "#7f1d1d", len(critical), "🚨 触发卖出 <40"),
    ]
    card_cells = ""
    for bg, text_color, count, label in stat_cards:
        card_cells += f'<td style="padding:6px;width:16.6%"><div style="background:{bg};border-radius:10px;padding:12px 6px;text-align:center;border:1px solid {text_color}22"><div style="font-size:24px;font-weight:800;color:{text_color};line-height:1.2">{count}</div><div style="font-size:11px;color:{text_color};margin-top:3px">{label}</div></div></td>'

    # ---- TOP5 / BOTTOM5 摘要 ----
    top5_tags = ""
    for r in results[:5]:
        top5_tags += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#fef3c7;font-size:12px;color:#92400e;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b></span>'
    bottom5_tags = ""
    for r in results[-5:]:
        bg_c = "#fecaca" if r["score"] < 40 else "#fee2e2"
        tc = "#7f1d1d" if r["score"] < 40 else "#991b1b"
        bottom5_tags += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:{bg_c};font-size:12px;color:{tc};font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b></span>'

    # ---- 各板块分段表格 ----
    section_html_parts = []
    # 各板块
    for sec_name, sec_stocks in sectors_by_count:
        sec_best = max(r["score"] for r in sec_stocks)
        sec_worst = min(r["score"] for r in sec_stocks)
        sec_buy = sum(1 for r in sec_stocks if r["sug_action"] in ("strong_buy", "buy"))
        sec_avoid = sum(1 for r in sec_stocks if r["sug_action"] == "avoid")
        anchor = f"sec-{hash(sec_name)%99999}"
        tags = ""
        if sec_buy > 0:
            tags += f'<span style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#dcfce7;color:#166534;font-weight:500">🟢 推荐{sec_buy}</span>'
        if sec_avoid > 0:
            tags += f'<span style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#fee2e2;color:#991b1b;font-weight:500">🔴 回避{sec_avoid}</span>'

        section_html_parts.append(f"""<div style="margin-bottom:20px" id="{anchor}">
<div style="background:#f8f9fc;padding:10px 16px;border:1px solid #e2e8f0;border-bottom:none;border-radius:8px 8px 0 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
<span style="font-size:14px;font-weight:600;color:#1a1a2e">📌 {sec_name}</span>
<span style="font-size:12px;color:#888">{len(sec_stocks)}只 · 最高{sec_best}/最低{sec_worst}</span>
{tags}
</div>
<div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;overflow-x:auto">
{make_table(sec_stocks, with_sector=False)}
</div>
</div>""")

    sections_html = chr(10).join(section_html_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS-Enhanced 股票评分日报 {today}</title></head>
<body style="margin:0;padding:0;background:#eef0f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f5"><tr><td align="center" style="padding:20px 12px">
<table width="900" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.08)">
<tr><td style="padding:24px 28px 14px;background:linear-gradient(135deg,#1a1a2e,#16213e)">
<h1 style="margin:0 0 6px;font-size:20px;color:#e94560">📊 SS-Enhanced 股票评分日报</h1>
<p style="margin:0;font-size:13px;color:#94a3b8"><strong style="color:#e6edf3">{today}</strong> &nbsp;|&nbsp; SS-Enhanced V2 &nbsp;|&nbsp; {len(results)}只A股 &nbsp;|&nbsp; 板块覆盖: {len(sectors)}个</p>
</td></tr>
<tr><td style="padding:16px 28px 8px">
<table width="100%" cellpadding="0" cellspacing="0"><tr>{card_cells}</tr></table>
<p style="font-size:12px;color:#888;margin-top:10px">主力板块: {', '.join(f'{s}({n})' for s, n in top_sectors)}</p>
</td></tr>
<tr><td style="padding:8px 28px 4px">
<h3 style="margin:8px 0 6px;font-size:14px;color:#92400e">🔥 最佳 TOP 5</h3>
<p style="margin:0 0 10px">{top5_tags}</p>
<h3 style="margin:8px 0 6px;font-size:14px;color:#991b1b">🔴 最差 BOTTOM 5</h3>
<p style="margin:0 0 10px">{bottom5_tags}</p>
</td></tr>
<tr><td style="padding:16px 28px 20px">
{sections_html}
</td></tr>
<tr><td style="padding:14px 28px 20px;text-align:center;border-top:1px solid #e8ecf1">
<p style="margin:0;font-size:11px;color:#999">自动生成 | SS-Enhanced V2 | 策略: ≥75强烈买入 · ≥70逢低买入 · ≥60持有 · ≥45观望 · &lt;45回避 · &lt;40触发卖出</p>
<p style="margin:4px 0 0;font-size:11px;color:#999">网格最优回测: 胜率52.9% · 均收益+24.9% · 夏普0.50</p>
</td></tr>
</table>
</td></tr></table>
</body></html>"""

    return html

# ========== 发送邮件（QQ邮箱 SMTP）==========
def send_email(md_path, email_html_path, today, recipient="914110627@qq.com"):
    """通过QQ邮箱SMTP发送报告邮件（使用邮件专用HTML，全内联样式无JS）"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    smtp_config = {
        "host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
    }
    
    try:
        with open(md_path) as f: md_content = f.read()
        with open(email_html_path) as f: html_content = f.read()
    except Exception as e:
        print(f"  [ERROR] 读取报告文件失败: {e}")
        return False
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 SS-Enhanced 股票评分日报 {today}"
    msg["From"] = smtp_config["user"]
    msg["To"] = recipient
    
    msg.attach(MIMEText(md_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    
    try:
        server = smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30)
        server.starttls()
        server.login(smtp_config["user"], smtp_config["password"])
        server.sendmail(smtp_config["user"], recipient, msg.as_string())
        server.quit()
        print(f"  ✅ 邮件已发送至 {recipient}")
        return True
    except Exception as e:
        print(f"  [ERROR] 邮件发送失败: {e}")
        return False

# ========== 主流程 ==========
def run_daily_scoring(stock_codes, output_dir="/workspace", send_mail=True, recipient=None):
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  SS-Enhanced 每日评分 [{today}]  ║")
    print(f"╚══════════════════════════════════════════╝")
    print(f"股票池: {len(stock_codes)} 只")
    
    # 加载历史评分 {code: {date: score}}
    hist_path = os.path.join(output_dir, "ss_score_history.json")
    hist_data = {}
    if os.path.exists(hist_path):
        try:
            with open(hist_path) as f: hist_data = json.load(f)
        except: pass
    
    # 转换为 {code: [score_d5, score_d4, ...]} 供趋势展示
    hist_scores = {}
    for code, date_scores in hist_data.items():
        hist_scores[code] = [s for d, s in sorted(date_scores.items())]
    
    print("\n[1/3] 获取数据...")
    klines = fetch_kline_batch(stock_codes)
    extra = fetch_extra_info(stock_codes)
    print(f"  K线: {len(klines)} | 行情: {len(extra)}")
    
    print("\n[2/3] 评分中...")
    results = []
    for code in stock_codes:
        kl = klines.get(code)
        if not kl or len(kl) < 60: continue
        ex = extra.get(code, {})
        s = score_ss_enhanced(kl, len(kl)-1)
        if s is None: continue
        ret_5d = (kl[-1]['close']/kl[-6]['close']-1)*100 if len(kl)>=6 else 0
        ret_20d = (kl[-1]['close']/kl[-21]['close']-1)*100 if len(kl)>=21 else 0
        sug_label, sug_action = get_suggestion(s["score"])
        results.append({
            "code":code,"name":ex.get("name",""),"price":ex.get("price",0),
            "change_pct":ex.get("change_pct",0),"ret_5d":ret_5d,"ret_20d":ret_20d,
            "sector": get_theme(code),
            "score":s["score"],"tech":s["tech"],"capital":s["capital"],"info":s["info"],
            "suggestion":sug_label,"sug_action":sug_action,
            "factors":s.get("factors",[]),
        })
        
        # 按日期存储评分（去重：同一天只保留最新）
        if code not in hist_data: hist_data[code] = {}
        hist_data[code][today] = s["score"]
        # 只保留最近15天
        sorted_dates = sorted(hist_data[code].keys())
        if len(sorted_dates) > 15:
            for old_date in sorted_dates[:-15]:
                del hist_data[code][old_date]
    
    results.sort(key=lambda x: x["score"], reverse=True)
    
    # 转换为趋势格式
    hist_scores = {}
    for code, date_scores in hist_data.items():
        hist_scores[code] = [s for d, s in sorted(date_scores.items())]
    
    # 保存历史评分
    with open(hist_path, "w") as f: json.dump(hist_data, f, ensure_ascii=False)
    
    print(f"\n[3/3] 生成报告...")
    sb=sum(1 for r in results if r["sug_action"]=="strong_buy")
    buy=sum(1 for r in results if r["sug_action"]=="buy")
    hold=sum(1 for r in results if r["sug_action"]=="hold")
    watch=sum(1 for r in results if r["sug_action"]=="watch")
    avoid=sum(1 for r in results if r["sug_action"]=="avoid")
    critical=sum(1 for r in results if r["score"]<40)
    print(f"  🔥强烈买入:{sb} 🟢逢低买入:{buy} 🟡持有:{hold} ⚪观望:{watch} 🔴回避:{avoid} 🚨触发卖出:{critical}")
    
    md_path, html_path, email_html_path = generate_report(results, today, output_dir, hist_scores)
    
    json_path = os.path.join(output_dir, f"SS增强版评分_{today}.json")
    with open(json_path, "w") as f:
        json.dump({"date":today,"model":"SS-Enhanced V2","total":len(results),"results":results},f,ensure_ascii=False,indent=2)
    
    if send_mail:
        print("\n[邮件] 发送报告...")
        send_email(md_path, email_html_path, today, recipient)
    
    print(f"\n{'='*50}")
    for r in results[:5]:
        if r["sug_action"] in ("strong_buy","buy"):
            print(f"  {r['code']} {r['name']}: {r['score']}分 {r['suggestion']}")
    
    return results

if __name__ == "__main__":
    # 项目目录（脚本所在目录）
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 股票代码文件：优先命令行参数，其次项目目录
    default_codes = os.path.join(PROJECT_DIR, "uploaded-stock-codes.txt")
    codes_file = sys.argv[1] if len(sys.argv) > 1 else default_codes
    with open(codes_file) as f: codes = [line.strip() for line in f if line.strip()]

    # 收件人：命令行第二个参数
    recipient = sys.argv[2] if len(sys.argv) > 2 else None

    # 输出目录：项目下的 output/
    run_daily_scoring(codes, output_dir=OUTPUT_DIR, recipient=recipient)
