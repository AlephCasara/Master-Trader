import {createChart, createSeriesMarkers, CandlestickSeries, ColorType, LineSeries, LineStyle, type UTCTimestamp, type AutoscaleInfoProvider, type IPriceLine, type ISeriesApi} from 'lightweight-charts';
import {themeColor, THEME_CHANGE} from './theme';
type Level={price:number;label:string;state?:string};
type Palette={bg:string;text:string;grid:string;up:string;down:string;entry:string;pending:string;level:string};
const palette=():Palette=>({bg:themeColor('--chart-bg'),text:themeColor('--chart-text'),grid:themeColor('--chart-grid'),up:themeColor('--chart-up'),down:themeColor('--chart-down'),entry:themeColor('--chart-entry'),pending:themeColor('--chart-pending'),level:themeColor('--chart-level')});
type Trade={open_ts?:number|string;is_short?:boolean;open_rate:number;close_rate:number;stop_rate?:number;is_open?:boolean;exit_levels?:Level[]};
type OHLC={time:UTCTimestamp;open:number;close:number;low:number;high:number};
// Overlay keys rendered as toggles; standard parameters, labeled as such in
// the UI — they visualize the mechanism, they are not the strategy's own
// dataframe values (those live inside the bot).
type OverlayKey='bb'|'ema'|'rsi';
const OVERLAY_META:{key:OverlayKey;label:string;color:string;dashed?:boolean;pane:number}[]=[
  {key:'bb',label:'BB(20,2)',color:'--chart-level',pane:0},
  {key:'ema',label:'EMA(200)',color:'--chart-up',pane:0},
  {key:'rsi',label:'RSI(14)',color:'--chart-pending',pane:1},
];
const sma=(v:number[],n:number)=>v.map((_,i)=>i<n-1?NaN:v.slice(i-n+1,i+1).reduce((s,x)=>s+x,0)/n);
const ema=(v:number[],n:number)=>{const k=2/(n+1);const out:number[]=[];let prev=NaN;for(let i=0;i<v.length;i++){prev=Number.isFinite(prev)?v[i]*k+prev*(1-k):v[i];out.push(prev);}return out;};
const rsi=(v:number[],n=14)=>{const out:number[]=[];let gain=0,loss=0;for(let i=0;i<v.length;i++){if(i===0){out.push(NaN);continue;}const d=v[i]-v[i-1];const g=Math.max(d,0),l=Math.max(-d,0);if(i<=n){gain+=g;loss+=l;if(i===n){gain/=n;loss/=n;out.push(loss===0?100:100-100/(1+gain/loss));}else out.push(NaN);continue;}gain=(gain*(n-1)+g)/n;loss=(loss*(n-1)+l)/n;out.push(loss===0?100:100-100/(1+gain/loss));}return out;};
class PriceChart {
  private chart; private series; private lines:IPriceLine[]=[]; private timeframe=''; private dead=false;
  private exits:Level[]=[]; private mode='price'; private markers; private entryIndex=-1; private colors:Palette;
  private overlays=new Map<string,ISeriesApi<'Line'>>();
  constructor(private el:HTMLElement) {
    const c=this.colors=palette();
    this.chart=createChart(el,{autoSize:true,localization:{locale:"en-US"},layout:{background:{type:ColorType.Solid,color:c.bg},textColor:c.text,fontSize:12,attributionLogo:true},grid:{vertLines:{visible:false},horzLines:{color:c.grid}},rightPriceScale:{borderVisible:false,scaleMargins:{top:.12,bottom:.12}},timeScale:{borderVisible:false,timeVisible:true,secondsVisible:false,rightOffset:4},crosshair:{mode:0}});
    this.series=this.chart.addSeries(CandlestickSeries,{upColor:c.up,downColor:c.down,borderVisible:false,wickUpColor:c.up,wickDownColor:c.down,lastValueVisible:false,priceLineVisible:false,autoscaleInfoProvider:((original)=>{const info=original();if(!info?.priceRange || this.mode!=='exits')return info;for(const x of this.exits){info.priceRange.minValue=Math.min(info.priceRange.minValue,x.price);info.priceRange.maxValue=Math.max(info.priceRange.maxValue,x.price);}return info;}) as AutoscaleInfoProvider});
    this.markers=createSeriesMarkers(this.series,[]);
    document.addEventListener(THEME_CHANGE,this.retheme);
  }
  private levelColor(state?:string){const c=this.colors;return state==='entry'?c.entry:state==='stop'?c.down:state==='active'?c.up:state==='pending'?c.pending:c.level;}
  private ensureOverlay(meta:{key:string;color:string;dashed?:boolean;pane:number}) {
    let s=this.overlays.get(meta.key);
    if(!s){
      try { s=this.chart.addSeries(LineSeries,{color:themeColor(meta.color),lineWidth:1,lineStyle:meta.dashed?LineStyle.Dashed:LineStyle.Solid,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false},meta.pane); }
      catch { return undefined; } // pane support unavailable: skip silently
      this.overlays.set(meta.key,s);
    }
    return s;
  }
  private retheme=()=>{
    if(this.dead)return;
    const c=this.colors=palette();
    this.chart.applyOptions({layout:{background:{type:ColorType.Solid,color:c.bg},textColor:c.text},grid:{horzLines:{color:c.grid}}});
    this.series.applyOptions({upColor:c.up,downColor:c.down,wickUpColor:c.up,wickDownColor:c.down});
    this.lines.forEach((line,i)=>line.applyOptions({color:this.levelColor(this.exits[i]?.state)}));
    this.markers.setMarkers(this.markers.markers().map(m=>({...m,color:c.entry})));
    this.overlays.forEach((s)=>s.applyOptions({color:themeColor(OVERLAY_META.find(m=>s===this.overlays.get(m.key))?.color||'--chart-level')}));
  };
  render(rows:number[][],trade:Trade,tf:string,mode:string,overlays:string[] = []) {
    const data=rows.filter(r=>r.length>=5 && r.every(Number.isFinite)).map(r=>({time:Math.floor(r[0]/1000) as UTCTimestamp,open:r[1],close:r[2],low:r[3],high:r[4]})).sort((a,b)=>a.time-b.time).filter((r,i,a)=>!i||r.time!==a[i-1].time);
    if(!data.length)return;
    const previous=this.chart.timeScale().getVisibleLogicalRange();
    this.mode=mode;
    this.exits=[{price:trade.open_rate,label:'Entry price',state:'entry'},{price:trade.close_rate,label:trade.is_open?'Current':'Exit'},...(trade.is_open && trade.stop_rate?[{price:trade.stop_rate,label:trade.is_open?'Bot stop':'Recorded stop',state:'stop'}]:[]),...(trade.exit_levels||[])].filter(x=>Number.isFinite(x.price)&&x.price>0);
    const reference=data[data.length-1].close;
    const precision=Math.min(10,Math.max(2,4-Math.floor(Math.log10(reference))));
    this.series.applyOptions({priceFormat:{type:'price',precision,minMove:10**-precision}});
    this.series.setData(data);
    this.lines.forEach(line=>this.series.removePriceLine(line));
    this.lines=this.exits.map(x=>this.series.createPriceLine({price:x.price,title:x.label,axisLabelVisible:true,color:this.levelColor(x.state),lineStyle:x.state==='entry'||x.state==='active'?LineStyle.Solid:LineStyle.Dotted,lineWidth:x.state==='entry'?2:1}));
    // Indicator overlays (bd ggb): computed here with standard parameters —
    // BB(20,2), EMA(200), Wilder RSI(14) — from the same venue candles. The
    // 'bb' key draws the middle band solid and the ±2σ bands dashed.
    const closes=data.map((d:OHLC)=>d.close);
    const want=new Set(overlays);
    const lines:Record<string,(number|undefined)[]>={};
    if(want.has('bb')){
      const mid=sma(closes,20);
      const variance=closes.map((_,i)=>i<19?NaN:closes.slice(i-19,i+1).reduce((s,x)=>s+x*x,0)/20-Math.pow(mid[i],2));
      const sd=variance.map(v=>Number.isFinite(v)?Math.sqrt(v):NaN);
      lines.bb_mid=mid;
      lines.bb_up=mid.map((m,i)=>Number.isFinite(m)?m+2*sd[i]:NaN);
      lines.bb_lo=mid.map((m,i)=>Number.isFinite(m)?m-2*sd[i]:NaN);
    }
    if(want.has('ema')) lines.ema=ema(closes,200);
    if(want.has('rsi')) lines.rsi=rsi(closes,14);
    for(const meta of OVERLAY_META){
      const series=this.ensureOverlay(meta);
      if(!series) continue;
      if(!want.has(meta.key)) { series.setData([]); continue; }
      const mk=(vals:(number|undefined)[])=>data.map((d,i)=>({time:d.time,value:vals[i]})).filter(p=>p.value!=null&&Number.isFinite(p.value));
      if(meta.key==='bb'){
        const sub:{suffix:string;dashed:boolean}[]=[{suffix:'mid',dashed:false},{suffix:'up',dashed:true},{suffix:'lo',dashed:true}];
        for(const {suffix,dashed} of sub){
          const s2=this.overlays.get('bb_'+suffix)||this.ensureOverlay({key:'bb_'+suffix,color:meta.color,pane:meta.pane});
          if(!s2) continue;
          s2.applyOptions({lineStyle:dashed?LineStyle.Dashed:LineStyle.Solid});
          s2.setData(mk(lines['bb_'+suffix]||[]));
        }
        series.setData([]); // the 'bb' key itself carries no line
      } else {
        series.setData(mk(lines[meta.key]||[]));
      }
    }
    // Hide any bb sub-series left over after a toggle-off.
    for(const key of ['bb_mid','bb_up','bb_lo']) if(!want.has('bb')) this.overlays.get(key)?.setData([]);
    const raw=trade.open_ts;
    const numeric=Number(raw);
    const opened=raw==null?NaN:Number.isFinite(numeric)?(numeric<1e12?numeric*1000:numeric):Date.parse(String(raw));
    const seconds=opened/1000;
    const duration=({"5m":300,"15m":900,"1h":3600,"4h":14400} as Record<string,number>)[tf];
    const candle=duration&&Number.isFinite(seconds)?data.find(r=>r.time<=seconds&&seconds<r.time+duration):undefined;
    this.entryIndex=candle?data.indexOf(candle):-1;
    this.markers.setMarkers(candle?[{time:candle.time,position:trade.is_short?'aboveBar':'belowBar',shape:trade.is_short?'arrowDown':'arrowUp',color:this.colors.entry,text:'Entry',size:1.5}]:[]);
    if(previous&&this.timeframe===tf)this.chart.timeScale().setVisibleLogicalRange(previous);
    else this.chart.timeScale().setVisibleLogicalRange({from:Math.max(0,data.length-90),to:data.length+3});
    this.timeframe=tf;
  }
  focusEntry(){if(this.entryIndex<0)return false;this.chart.timeScale().setVisibleLogicalRange({from:Math.max(0,this.entryIndex-12),to:this.entryIndex+12});return true;}
  getDom(){return this.el;} isDisposed(){return this.dead;}
  resize(){if(!this.dead)this.chart.resize(this.el.clientWidth,this.el.clientHeight);}
  dispose(){this.dead=true;document.removeEventListener(THEME_CHANGE,this.retheme);this.chart.remove();}
}
(window as unknown as {TradingPriceChart:typeof PriceChart}).TradingPriceChart=PriceChart;

import "./analytics";
