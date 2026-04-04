// This work is licensed under a Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0) https://creativecommons.org/licenses/by-nc-sa/4.0/
// © LuxAlgo

//@version=6
indicator("HTF Volume Spike & Imbalance Projection [LuxAlgo]", "LuxAlgo - Advanced Spike Projection", overlay = true, max_boxes_count = 500, max_lines_count = 500, max_labels_count = 500)

//---------------------------------------------------------------------------------------------------------------------}
// Settings
//---------------------------------------------------------------------------------------------------------------------{
string HTF_GRP      = "Higher Timeframe (Anchor)"
string SPIKE_GRP    = "Volume Spike Detection"
string FEAT_GRP     = "Advanced Features"
string VIS_GRP      = "Visuals"

// Auto Timeframe Logic Functions
getAutoHtf() =>
    if timeframe.isintraday
        if timeframe.multiplier < 5
            "15"
        else if timeframe.multiplier < 15
            "60"
        else if timeframe.multiplier < 60
            "240"
        else
            "D"
    else if timeframe.isdaily
        "W"
    else
        "M"

getAutoLtf() =>
    if timeframe.isintraday
        if timeframe.multiplier >= 60
            "5"
        else
            "1"
    else if timeframe.isdaily
        "60"
    else
        "240"

// Inputs
string htfInput     = input.string("Auto", "HTF Anchor Timeframe", group = HTF_GRP, options = ["Auto", "15", "30", "60", "240", "D", "W", "M"])
string ltfInput     = input.string("Auto", "Spike Granularity",    group = SPIKE_GRP, options = ["Auto", "1", "3", "5", "15", "60", "240"])
float spikeMultInput= input.float(1.2,      "Volume Spike Multiplier", minval = 1.0, step = 0.1, group = SPIKE_GRP)
int maLenInput      = input.int(20,         "Volume MA Length", minval = 5, group = SPIKE_GRP)

bool showGhostInput = input.bool(true,      "Show Ghost (Previous) Bar", group = FEAT_GRP)
bool showImbInput   = input.bool(true,      "Highlight Stacked Imbalances", group = FEAT_GRP)
bool showHtfConnLines=input.bool(true,      "Show Anchor Connection Lines", group = FEAT_GRP)
bool showChartSpikes= input.bool(true,      "Show Bubbles on Chart Candles", group = FEAT_GRP)
bool showChartImb   = input.bool(true,      "Show Imbalances on Chart Candles", group = FEAT_GRP)

int projOffsetInput = input.int(5,          "Projection X-Offset", minval = 1, group = VIS_GRP)
int scatterWidthInput=input.int(25,         "Scatter Plot Width", minval = 10, group = VIS_GRP)
int vpRowsInput     = input.int(40,         "VP Row Density", minval = 10, group = VIS_GRP)
int vpWidthInput    = input.int(12,         "VP Max Width (Bars)", minval = 5, group = VIS_GRP)

color bullColorInput    = input.color(#089981, "Bullish Color", inline = "col")
color bearColorInput    = input.color(#f23645, "Bearish Color", inline = "col")
color projBgColorInput  = input.color(color.new(#2a2e39, 60), "Projection Background")

// Resolve Timeframes
string tfHtf = htfInput == "Auto" ? getAutoHtf() : htfInput
string tfLtf = ltfInput == "Auto" ? getAutoLtf() : ltfInput
string displayTf = tfHtf == "D" ? "Daily" : tfHtf == "W" ? "Weekly" : tfHtf == "M" ? "Monthly" : tfHtf + "m"

//---------------------------------------------------------------------------------------------------------------------}
// Data Retrieval & State Management
//---------------------------------------------------------------------------------------------------------------------{
bool isNewHtf = ta.change(time(tfHtf)) != 0

// State
var float htfOpen = na, var float htfHigh = na, var float htfLow = na, var float htfClose = na
var int htfOpenIdx = na, var int htfHighIdx = na, var int htfLowIdx = na
var spikePriceArr = array.new<float>(), var spikeTimeArr = array.new<int>(), var spikeVolArr = array.new<float>(), var spikeDeltaArr = array.new<float>()
var htfCloseArr = array.new<float>(), var htfVolArr = array.new<float>()

// Archive
var float pOpen = na, var float pHigh = na, var float pLow = na, var float pClose = na
var int pHtfOpenIdx = na
var pSPriceArr = array.new<float>(), var pSTimeArr = array.new<int>(), var pSVolArr = array.new<float>(), var pSDeltaArr = array.new<float>()
var pHCloseArr = array.new<float>(), var pHVolArr = array.new<float>()

// Drawing management
var boxArr = array.new<box>(), var lineArr = array.new<line>(), var labelArr = array.new<label>(), var chartSpikeArr = array.new<label>(), var chartImbArr = array.new<box>()

if isNewHtf
    pOpen := htfOpen, pHigh := htfHigh, pLow := htfLow, pClose := htfClose
    pHtfOpenIdx := htfOpenIdx
    pSPriceArr := spikePriceArr.copy(), pSTimeArr := spikeTimeArr.copy(), pSVolArr := spikeVolArr.copy(), pSDeltaArr := spikeDeltaArr.copy()
    pHCloseArr := htfCloseArr.copy(), pHVolArr := htfVolArr.copy()

    htfOpen := open, htfHigh := high, htfLow := low, htfClose := close
    htfOpenIdx := bar_index, htfHighIdx := bar_index, htfLowIdx := bar_index
    spikePriceArr.clear(), spikeTimeArr.clear(), spikeVolArr.clear(), spikeDeltaArr.clear(), htfCloseArr.clear(), htfVolArr.clear()
else
    if high >= nz(htfHigh, -1.0e10)
        htfHigh := high
        htfHighIdx := bar_index
    if low <= nz(htfLow, 1.0e10)
        htfLow := low
        htfLowIdx := bar_index
    htfClose := close

[ltfO, ltfC, ltfV, ltfVma] = request.security_lower_tf(syminfo.tickerid, tfLtf, [open, close, volume, ta.sma(volume, maLenInput)])

if not na(ltfO) and ltfO.size() > 0
    for i = 0 to array.size(ltfO) - 1
        float o = array.get(ltfO, i), float c = array.get(ltfC, i), float v = array.get(ltfV, i), float vma = array.get(ltfVma, i)
        if htfCloseArr.size() < 450
            htfCloseArr.push(c), htfVolArr.push(v)
            if not na(vma) and v > vma * spikeMultInput
                spikePriceArr.push(c), spikeTimeArr.push(bar_index), spikeVolArr.push(v), spikeDeltaArr.push(c >= o ? 1.0 : -1.0)

//---------------------------------------------------------------------------------------------------------------------}
// Rendering Logic
//---------------------------------------------------------------------------------------------------------------------{
clearDrawings() =>
    if boxArr.size() > 0 
        for b in boxArr 
            b.delete()
        boxArr.clear()
    if lineArr.size() > 0 
        for l in lineArr 
            l.delete()
        lineArr.clear()
    if labelArr.size() > 0 
        for lb in labelArr 
            lb.delete()
        labelArr.clear()
    if chartSpikeArr.size() > 0 
        for lb in chartSpikeArr 
            lb.delete()
        chartSpikeArr.clear()
    if chartImbArr.size() > 0 
        for b in chartImbArr 
            b.delete()
        chartImbArr.clear()

renderChartBubbles(array<float> sP, array<int> sT, array<float> sV, array<float> sD, int transp) =>
    if sP.size() > 0
        float maxV = sV.max(), float minV = sV.min()
        for i = 0 to sP.size() - 1
            float p = sP.get(i), float v = sV.get(i), int t = sT.get(i), float d = sD.get(i)
            float relV = maxV != minV ? (v - minV) / (maxV - minV) : 1.0
            string bSz = relV < 0.25 ? size.tiny : relV < 0.5 ? size.small : relV < 0.8 ? size.normal : size.large
            color baseC = d > 0 ? bullColorInput : bearColorInput
            color bCol = color.from_gradient(relV, 0, 1.0, color.new(baseC, 65 + transp/2), color.new(baseC, 5 + transp/2))
            chartSpikeArr.push(label.new(t, p, "●", color = #00000000, textcolor = bCol, style = label.style_label_center, size = bSz))

renderChartImbalances(float h, float l, int startIdx, int endIdx, array<float> sP, array<float> sD, int transp) =>
    if sP.size() > 0 and showChartImb and h > l
        float step = (h - l) / vpRowsInput
        for r = 0 to vpRowsInput - 1
            float bL = l + (r * step), float bH = bL + step
            int buyCount = 0, int sellCount = 0
            for i = 0 to sP.size() - 1
                if sP.get(i) >= bL and sP.get(i) < bH
                    if sD.get(i) > 0 
                        buyCount += 1 
                    else 
                        sellCount += 1
            if buyCount >= 3 or sellCount >= 3
                color imbC = buyCount >= 3 ? color.new(bullColorInput, 80 + transp/4) : color.new(bearColorInput, 80 + transp/4)
                chartImbArr.push(box.new(startIdx, bL, endIdx, bH, border_color = na, bgcolor = imbC))

renderBlock(float o, float h, float l, float c, array<float> sP, array<float> sV, array<float> sD, array<float> hC, array<float> hV, int startX, int transp, string title) =>
    int candleWidth = 6
    int htfRight = startX + candleWidth
    color hColor = c >= o ? bullColorInput : bearColorInput
    color hColorTransp = color.new(hColor, transp)
    
    labelArr.push(label.new(int(math.avg(startX, htfRight)), h, title, style = label.style_none, textcolor = color.new(chart.fg_color, transp), size = size.small, text_font_family = font.family_monospace, textalign = text.align_center))

    boxArr.push(box.new(startX, o, htfRight, c, border_color = hColorTransp, bgcolor = color.new(hColorTransp, 80)))
    lineArr.push(line.new(int(math.avg(startX, htfRight)), h, int(math.avg(startX, htfRight)), math.max(o, c), color = hColorTransp))
    lineArr.push(line.new(int(math.avg(startX, htfRight)), l, int(math.avg(startX, htfRight)), math.min(o, c), color = hColorTransp))
    
    int sStartX = htfRight + 8, int sEndX = sStartX + scatterWidthInput
    if hC.size() > 0 and h > l
        boxArr.push(box.new(sStartX, h, sEndX, l, border_color = na, bgcolor = color.new(projBgColorInput, transp)))
        lineArr.push(line.new(htfRight, h, sStartX, h, color = color.new(chart.fg_color, 85 + transp/2), style = line.style_dotted))
        lineArr.push(line.new(htfRight, l, sStartX, l, color = color.new(chart.fg_color, 85 + transp/2), style = line.style_dotted))

        // Imbalances
        if showImbInput and sP.size() > 0
            float step = (h - l) / vpRowsInput
            for r = 0 to vpRowsInput - 1
                float bL = l + (r * step), float bH = bL + step
                int buyCount = 0, int sellCount = 0
                for i = 0 to sP.size() - 1
                    if sP.get(i) >= bL and sP.get(i) < bH
                        if sD.get(i) > 0 
                            buyCount += 1 
                        else 
                            sellCount += 1
                if buyCount >= 3 or sellCount >= 3
                    color imbC = buyCount >= 3 ? color.new(bullColorInput, 75 + transp/2) : color.new(bearColorInput, 75 + transp/2)
                    boxArr.push(box.new(sStartX, bL, sEndX, bH, border_color = na, bgcolor = imbC))

        // Spikes
        if sP.size() > 0
            float maxV = sV.max(), float minV = sV.min()
            for i = 0 to sP.size() - 1
                float p = sP.get(i), float v = sV.get(i)
                int scX = sStartX + int((float(i) / math.max(1, hC.size() - 1)) * scatterWidthInput)
                float relV = maxV != minV ? (v - minV) / (maxV - minV) : 1.0
                string bSz = relV < 0.25 ? size.tiny : relV < 0.5 ? size.small : relV < 0.8 ? size.normal : size.large
                color baseC = sD.get(i) > 0 ? bullColorInput : bearColorInput
                color bCol = color.from_gradient(relV, 0, 1.0, color.new(baseC, 65 + transp/2), color.new(baseC, 5 + transp/2))
                labelArr.push(label.new(scX, p, "●", color = #00000000, textcolor = bCol, style = label.style_label_center, size = bSz))

    // Volume Profile Calculation
    float bStep = (h - l) / vpRowsInput
    array<float> bins = array.new<float>(vpRowsInput, 0.0)
    int finalX = sEndX
    if bStep > 0 and hC.size() > 0
        for i = 0 to hC.size() - 1
            int bIdx = math.max(0, math.min(vpRowsInput - 1, int(math.floor((hC.get(i) - l) / bStep))))
            bins.set(bIdx, bins.get(bIdx) + hV.get(i))
        
        float maxV = bins.size() > 0 ? bins.max() : 0
        if maxV > 0
            int vpX = sEndX + 3
            finalX := vpX + vpWidthInput
            for i = 0 to vpRowsInput - 1
                float vol = bins.get(i)
                if vol > 0
                    int rW = int((vol / maxV) * vpWidthInput)
                    color vC = vol == maxV ? color.new(hColor, 10 + transp/4) : color.from_gradient(vol, 0, maxV, color.new(hColor, 90 + transp/4), color.new(hColor, 40 + transp/4))
                    boxArr.push(box.new(vpX, l + (i * bStep), vpX + rW, l + (i * bStep) + bStep, border_color = na, bgcolor = vC))
    finalX

//---------------------------------------------------------------------------------------------------------------------}
// Main Execution
//---------------------------------------------------------------------------------------------------------------------{
if barstate.islast
    clearDrawings()
    int nextX = bar_index + projOffsetInput
    if showGhostInput and not na(pOpen)
        nextX := renderBlock(pOpen, pHigh, pLow, pClose, pSPriceArr, pSVolArr, pSDeltaArr, pHCloseArr, pHVolArr, nextX, 75, displayTf + " (Prev)") + 12
        renderChartBubbles(pSPriceArr, pSTimeArr, pSVolArr, pSDeltaArr, 75)
        renderChartImbalances(pHigh, pLow, pHtfOpenIdx, htfOpenIdx - 1, pSPriceArr, pSDeltaArr, 75)

    int currStartX = nextX
    int currEndX = renderBlock(htfOpen, htfHigh, htfLow, htfClose, spikePriceArr, spikeVolArr, spikeDeltaArr, htfCloseArr, htfVolArr, currStartX, 0, displayTf)
    renderChartBubbles(spikePriceArr, spikeTimeArr, spikeVolArr, spikeDeltaArr, 0)
    renderChartImbalances(htfHigh, htfLow, htfOpenIdx, bar_index, spikePriceArr, spikeDeltaArr, 0)
    
    if showHtfConnLines
        lineArr.push(line.new(htfOpenIdx, htfOpen, currStartX, htfOpen, color = color.new(chart.fg_color, 85), style = line.style_dashed))
        lineArr.push(line.new(htfHighIdx, htfHigh, currStartX, htfHigh, color = color.new(bullColorInput, 85), style = line.style_dashed))
        lineArr.push(line.new(htfLowIdx,  htfLow,  currStartX, htfLow,  color = color.new(bearColorInput, 85), style = line.style_dashed))
        lineArr.push(line.new(bar_index,  htfClose,currStartX, htfClose,color = color.new(htfClose >= htfOpen ? bullColorInput : bearColorInput, 85), style = line.style_dashed))

    int labelX = currEndX + 4
    labelArr.push(label.new(labelX, htfHigh,  "H: " + str.tostring(htfHigh, format.mintick), style = label.style_none, textcolor = bullColorInput, size = size.small, text_font_family = font.family_monospace))
    labelArr.push(label.new(labelX, htfOpen,  "O: " + str.tostring(htfOpen, format.mintick), style = label.style_none, textcolor = chart.fg_color, size = size.small, text_font_family = font.family_monospace))
    labelArr.push(label.new(labelX, htfClose, "C: " + str.tostring(htfClose, format.mintick),style = label.style_none, textcolor = htfClose >= htfOpen ? bullColorInput : bearColorInput, size = size.small, text_font_family = font.family_monospace))
    labelArr.push(label.new(labelX, htfLow,   "L: " + str.tostring(htfLow, format.mintick),  style = label.style_none, textcolor = bearColorInput, size = size.small, text_font_family = font.family_monospace))

//---------------------------------------------------------------------------------------------------------------------}