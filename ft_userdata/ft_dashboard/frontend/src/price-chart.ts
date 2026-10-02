import {createChart, createSeriesMarkers, CandlestickSeries, ColorType, LineStyle, type UTCTimestamp, type AutoscaleInfoProvider, type IPriceLine} from 'lightweight-charts';
import {themeColor, THEME_CHANGE} from './theme';
type Level={price:number;label:string;state?:string};
type Palette={bg:string;text:string;grid:string;up:string;down:string;entry:string;pending:string;level:string};
const palette=():Palette=>({bg:themeColor('--chart-bg'),text:themeColor('--chart-text'),grid:themeColor('--chart-grid'),up:themeColor('--chart-up'),down:themeColor('--chart-down'),entry:themeColor('--chart-entry'),pending:themeColor('--chart-pending'),level:themeColor('--chart-level')});
type Trade={open_ts?:number|string;is_short?:boolean;open_rate:number;close_rate:number;stop_rate?:number;is_open?:boolean;exit_levels?:Level[]};
class PriceChart {
  private chart; private series; private lines:IPriceLine[]=[]; private timeframe=''; private dead=false;
  private exits:Level[]=[]; private mode='price'; private markers; private entryIndex=-1; private colors:Palette;
  constructor(private el:HTMLElement) {
    const c=this.colors=palette();
    this.chart=createChart(el,{autoSize:true,localization:{locale:"en-US"},layout:{background:{type:ColorType.Solid,color:c.bg},textColor:c.text,fontSize:12,attributionLogo:true},grid:{vertLines:{visible:false},horzLines:{color:c.grid}},rightPriceScale:{borderVisible:false,scaleMargins:{top:.12,bottom:.12}},timeScale:{borderVisible:false,timeVisible:true,secondsVisible:false,rightOffset:4},crosshair:{mode:0}});
    this.series=this.chart.addSeries(CandlestickSeries,{upColor:c.up,downColor:c.down,borderVisible:false,wickUpColor:c.up,wickDownColor:c.down,lastValueVisible:false,priceLineVisible:false,autoscaleInfoProvider:((original)=>{const info=original();if(!info?.priceRange || this.mode!=='exits')return info;for(const x of this.exits){info.priceRange.minValue=Math.min(info.priceRange.minValue,x.price);info.priceRange.maxValue=Math.max(info.priceRange.maxValue,x.price);}return info;}) as AutoscaleInfoProvider});
    this.markers=createSeriesMarkers(this.series,[]);
    document.addEventListener(THEME_CHANGE,this.retheme);
  }
  private levelColor(state?:string){const c=this.colors;return state==='entry'?c.entry:state==='stop'?c.down:state==='active'?c.up:state==='pending'?c.pending:c.level;}
  private retheme=()=>{
    if(this.dead)return;
    const c=this.colors=palette();
    this.chart.applyOptions({layout:{background:{type:ColorType.Solid,color:c.bg},textColor:c.text},grid:{horzLines:{color:c.grid}}});
    this.series.applyOptions({upColor:c.up,downColor:c.down,wickUpColor:c.up,wickDownColor:c.down});
    this.lines.forEach((line,i)=>line.applyOptions({color:this.levelColor(this.exits[i]?.state)}));
    this.markers.setMarkers(this.markers.markers().map(m=>({...m,color:c.entry})));
  };
  render(rows:number[][],trade:Trade,tf:string,mode:string) {
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
