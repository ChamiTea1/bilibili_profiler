// 报告页前端逻辑（原 web.py VIDEO_JS 平移；数据经 window.__DATA__ 注入，本文件须在其后加载）
// 页面数据由服务端内联 <script>window.__DATA__</script> 注入（工程结构改造，替代占位符 .replace）
const PAGE_DATA = window.__DATA__ || {};

// 标签页切换
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
    // 标签页写入 URL hash（spec 5）：刷新/分享链接可回到同一标签页
    if (location.hash !== '#tab=' + name) history.replaceState(null, '', '#tab=' + name);
    // 争执焦点连线（隐藏时坐标为0，切到该标签页可见后重算）
    if (name === 'attack') requestAnimationFrame(drawAfEdges);
}

// 从 URL hash 还原标签页（仅接受存在的标签名）
function restoreTabFromHash() {
    const m = location.hash.match(/tab=(\w+)/);
    if (m && document.querySelector('.tab-btn[data-tab="' + m[1] + '"]')) switchTab(m[1]);
}

// 概览图表（默认宽高比自适应，不写死高度）
const chartData = PAGE_DATA.chart;
new Chart(document.getElementById('levelChart'), {type:'bar',
    data:{labels:chartData.level_labels, datasets:[{label:'人数', data:chartData.level_data, backgroundColor:'#00a1d6', borderRadius:6}]},
    options:{responsive:true, plugins:{legend:{display:false}}}});
new Chart(document.getElementById('spamChart'), {type:'doughnut',
    data:{labels:['低风险','中风险','高风险'], datasets:[{data:chartData.spam_data, backgroundColor:['#4caf50','#ff9800','#f44336']}]},
    options:{responsive:true}});
new Chart(document.getElementById('tagChart'), {type:'bar',
    data:{labels:chartData.tag_labels, datasets:[{label:'出现次数', data:chartData.tag_data, backgroundColor:'#ff9f43', borderRadius:6}]},
    options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
// 地域分布并入用户标签卡：无属地数据时服务端不渲染 regionChart canvas，须判空
const regionCanvas = document.getElementById('regionChart');
if (regionCanvas && chartData.region_labels.length) {
    new Chart(regionCanvas, {type:'bar',
        data:{labels:chartData.region_labels, datasets:[{label:'人数', data:chartData.region_data, backgroundColor:'#fb7299', borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
}

// 弹幕密度时间轴（概览页宽幅；无全量弹幕数据的旧视频无此 canvas）
// 分P视频按分P分别建桶（弹幕 time 是各P内部时间，与全片累计时长不可混用），
// 故多分P时先出分P选择器、默认选中弹幕最多的那P；点击柱条跳转该分P对应时段核验（P1-a）
const densityData = PAGE_DATA.density;
const densityCanvas = document.getElementById('densityChart');
if (densityCanvas && densityData && (densityData.pages || []).length) {
    const densityPager = document.getElementById('densityPager');
    const densityPages = densityData.pages;
    let densityChart = null;
    const drawDensity = (idx) => {
        const p = densityPages[idx];
        const tag = (densityData.multi ? 'P' + p.page + (p.part ? ' ' + p.part : '') : (p.part || ''));
        if (densityChart) densityChart.destroy();   // 切换分P需销毁旧实例，否则 canvas 复用出错
        densityChart = new Chart(densityCanvas, {type:'bar',
            data:{labels:p.labels, datasets:[{label:'弹幕数', data:p.data, backgroundColor:'#00a1d6', borderRadius:2}]},
            options:{responsive:true, aspectRatio:4,
                onHover:(e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
                onClick:(e, els) => {
                    if (!els.length) return;
                    const t = (p.starts || [])[els[0].index] || 0;
                    // 分P视频必须带 p 参数，否则跳到 P1 的错误时段
                    const query = (densityData.multi ? 'p=' + p.page + '&' : '') + 't=' + t;
                    window.open('https://www.bilibili.com/video/' + PAGE_DATA.bvid + '?' + query, '_blank', 'noopener');
                },
                plugins:{legend:{display:false}, tooltip:{callbacks:{title: items => (tag ? tag + ' · ' : '') + '视频时间 ' + items[0].label + '（点击跳转）'}}},
                scales:{x:{ticks:{autoSkip:true, maxTicksLimit:20}}, y:{beginAtZero:true}}}});
        if (densityPager) {
            densityPager.querySelectorAll('button').forEach((b, i) => b.classList.toggle('active', i === idx));
        }
    };
    if (densityPager && densityPages.length > 1) {
        densityPages.forEach((p, i) => {
            const total = p.data.reduce((a, b) => a + b, 0);
            const part = p.part || '';
            const short = part.length > 12 ? part.slice(0, 12) + '…' : part;
            const btn = document.createElement('button');
            btn.className = 'pager-btn';
            btn.textContent = 'P' + p.page + (short ? ' ' + short : '') + ' · ' + total.toLocaleString() + ' 条';
            btn.title = (part || '分P ' + p.page) + '（时长 ' + p.duration + ' 秒 · 弹幕 ' + total + ' 条）';
            btn.onclick = () => drawDensity(i);
            densityPager.appendChild(btn);
        });
    }
    drawDensity(Math.min(densityData.default || 0, densityPages.length - 1));
}

// 解析质量区块（概览页）：解析方式分布 + 置信度分布
const rqData = PAGE_DATA.resolveQuality;
if (rqData && document.getElementById('rqMethodChart')) {
    new Chart(document.getElementById('rqMethodChart'), {type:'doughnut',
        data:{labels:rqData.method_labels, datasets:[{data:rqData.method_data,
              backgroundColor:['#00a1d6','#66bb6a','#ff9f43','#ab47bc','#ef5350','#8d6e63','#90a4ae']}]},
        options:{responsive:true}});
    const confColors = {'高':'#4caf50','中':'#ff9800','低':'#f44336','无':'#bdbdbd'};
    new Chart(document.getElementById('rqConfChart'), {type:'bar',
        data:{labels:rqData.conf_labels, datasets:[{label:'人数', data:rqData.conf_data,
              backgroundColor:rqData.conf_labels.map(c => confColors[c] || '#bdbdbd'), borderRadius:6}]},
        options:{responsive:true, plugins:{legend:{display:false}}}});
}

// 问题弹幕类别分布小图（弹幕浏览器统计面板；无弹幕数据的旧视频无此 canvas）
const dmCatData = PAGE_DATA.categories;
const dmCatColors = PAGE_DATA.categoryColors;
const dmCatCanvas = document.getElementById('dmCatChart');
if (dmCatCanvas) {
    const catLabels = Object.keys(dmCatData);
    new Chart(dmCatCanvas, {type:'bar',
        data:{labels:catLabels, datasets:[{label:'命中人数', data:catLabels.map(k => dmCatData[k]),
              backgroundColor:catLabels.map(k => dmCatColors[k] || '#999'), borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
}

// 弹幕模式分布小图（弹幕浏览器统计面板；mode 已入库：滚动/顶部/底部/其他）
const dmModeData = PAGE_DATA.dmMode;
const dmModeCanvas = document.getElementById('dmModeChart');
if (dmModeCanvas && dmModeData) {
    new Chart(dmModeCanvas, {type:'doughnut',
        data:{labels:Object.keys(dmModeData), datasets:[{data:Object.values(dmModeData),
              backgroundColor:['#00a1d6','#fb7299','#ff9f43','#90a4ae']}]},
        options:{responsive:true}});
}

// 高回复评论：回复树默认折叠到固定高度，展开/折叠切换
function hotToggle(btn) {
    const box = btn.previousElementSibling;
    const collapsed = box.classList.toggle('collapsed');
    btn.textContent = collapsed ? '展开全部回复 ▾' : '收起回复 ▴';
}

// 误报标记（P2-a）：问题弹幕（kind=dm，target=内容）/问题评论（kind=cmt，target=rpid）/
// 刷屏判定（kind=spam，target=mid_hash）。切换人工误报标记；标记后该条不再计入聚合与
// 用户疑似分（刷屏判定降级为低风险展示），可撤销。
// kind=dm 按内容全文标记：一次标记隐藏全部同名弹幕，confirm 前先做只读查询提示影响面
function fpToggle(btn) {
    const kind = btn.dataset.kind, target = btn.dataset.target;
    const willMark = !btn.classList.contains('fp-btn-marked');
    const shown = target.length > 50 ? target.slice(0, 50) + '…' : target;
    const doToggle = affected => {
        // 影响面提示：kind=dm 时一次操作作用于全部同名弹幕
        const impact = (kind === 'dm' && affected > 1)
            ? '\n注意：将' + (willMark ? '隐藏' : '恢复') + '本视频全部 ' + affected + ' 条同名弹幕。' : '';
        const what = (kind === 'dm' ? '该条弹幕内容'
            : (kind === 'spam' ? '该发送者的刷屏判定' : '该条评论'));
        const msg = willMark
            ? '将' + what + '标记为误报？标记后不再计入聚合。' + impact + '\n\n' + shown
            : '撤销该条的误报标记？撤销后重新计入聚合。' + impact + '\n\n' + shown;
        if (!confirm(msg)) return;
        btn.disabled = true;
        fetch('/api/video/' + encodeURIComponent(BVID) + '/false_positive', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({kind: kind, target: target})
        }).then(r => r.json().then(j => ({ok: r.ok, j})))
          .then(({ok, j}) => {
              if (!ok) { alert(j.error || '标记失败'); btn.disabled = false; return; }
              location.reload();   // 聚合已变（页缓存服务端已失效），整页刷新最简且一致
          })
          .catch(() => { alert('网络错误'); btn.disabled = false; });
    };
    if (kind === 'dm') {
        // 先只读查询影响面（count_only），confirm 文案带上将隐藏/恢复的同名弹幕条数
        fetch('/api/video/' + encodeURIComponent(BVID) + '/false_positive', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({kind: kind, target: target, count_only: true})
        }).then(r => r.json()).then(j => doToggle(j.affected || 0))
          .catch(() => doToggle(0));   // 查询失败降级为不带条数的确认框
    } else {
        doToggle(1);   // kind=cmt/spam 只影响该条评论/该发送者本身
    }
}

// UP主悬停词云弹窗：旧版缓存画像的词频随页面注入（upWcData）；新口径采集阶段只存关注名单，
// 词云在悬停时经 /api/up/<uid>/wordcloud 懒加载（服务端 llm_cache 键 up:{uid} 缓存复用）
const upWcData = PAGE_DATA.upWordcloud || {};
const upWcCache = {};   // 词云结果前端缓存：upUid → words
const upWcInflight = {};   // upUid → Promise（在途去重：预热与悬停共享同一请求）
const popup = document.getElementById('wc-popup');
const popupCanvas = document.getElementById('wc-popup-canvas');

function fetchUpWc(upUid) {
    /* 取词云（带在途去重与缓存）：预热队列和悬停 handler 共用 */
    if (upWcCache[upUid]) return Promise.resolve(upWcCache[upUid]);
    if (upWcInflight[upUid]) return upWcInflight[upUid];
    upWcInflight[upUid] = fetch('/api/up/' + encodeURIComponent(upUid) + '/wordcloud')
        .then(r => r.json())
        .then(j => {
            if (j && j.words && j.words.length) { upWcCache[upUid] = j.words; return j.words; }
            return null;
        })
        .catch(() => null)
        .finally(() => { delete upWcInflight[upUid]; });
    return upWcInflight[upUid];
}

// 视口预热：进入视口附近（200px 余量）的 chip 进入慢速队列逐个预取（间隔 700ms，
// 与批量采集的限速节奏相当），用户悬停时多半已命中前端/服务端缓存而瞬开
const wcWarmQueue = [];
const wcWarmQueued = new Set();
let wcWarmTimer = null;
let wcWarming = false;   // 消费泵是否在途：wcWarmTimer 在 fetch pending 期间为 null，观察器仅凭 timer 判定会并发第二支泵
function wcWarmNext() {
    const uid = wcWarmQueue.shift();
    if (!uid) { wcWarmTimer = null; wcWarming = false; return; }   // 队列耗尽不再安排下一轮：释放泵标志，后续触发可重启
    fetchUpWc(uid).finally(() => { wcWarmTimer = setTimeout(wcWarmNext, 700); });
}
const wcObserver = new IntersectionObserver(entries => {
    for (const en of entries) {
        const uid = en.target.dataset && en.target.dataset.upUid;
        if (en.isIntersecting && uid && !upWcCache[uid] && !wcWarmQueued.has(uid)) {
            wcWarmQueued.add(uid);
            wcWarmQueue.push(uid);
        }
    }
    // 以 wcWarming 看守整个泵生命周期（含 fetch pending 窗口）：密集滚动多次触发也只保留一支泵，守住 700ms 节流
    if (wcWarmQueue.length && !wcWarming) { wcWarming = true; wcWarmNext(); }
}, {rootMargin: '200px'});
document.querySelectorAll('.up-chip[data-up-uid]').forEach(c => wcObserver.observe(c));

function showUpWc(chip, data) {
    if (!data || data.length === 0) return;
    const rect = chip.getBoundingClientRect();
    popup.style.display = 'block';
    popup.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
    popup.style.top = (rect.bottom + 8) + 'px';
    const maxW = Math.max(...data.map(d => d[1]));
    const minW = Math.min(...data.map(d => d[1]));
    const scaled = data.map(d => [d[0], 10 + (d[1] - minW) / Math.max(maxW - minW, 1) * 50]);
    WordCloud(popupCanvas, {list: scaled, gridSize: 10, weightFactor: 1, fontFamily: 'sans-serif',
        color: () => ['#00a1d6','#fb7299','#ff9f43','#6c5ce7','#2e7d32'][Math.floor(Math.random()*5)],
        rotateRatio: 0, backgroundColor: '#ffffff', shape: 'circle', clearCanvas: true});
}

function showUpWcText(chip, text) {   // 加载中/失败等纯文本提示（画在词云画布上）
    const rect = chip.getBoundingClientRect();
    popup.style.display = 'block';
    popup.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
    popup.style.top = (rect.bottom + 8) + 'px';
    const ctx = popupCanvas.getContext('2d');
    ctx.clearRect(0, 0, popupCanvas.width, popupCanvas.height);
    ctx.font = '14px sans-serif';
    ctx.fillStyle = '#888';
    ctx.fillText(text, 10, 30);
}

document.querySelectorAll('.up-chip').forEach(chip => {
    // 点击 chip 新标签页打开该 UP 主空间主页；data-up-uid 缺失（旧缓存画像）则不响应
    if (chip.dataset.upUid) {
        chip.title = (chip.title ? chip.title + ' | ' : '') + '点击打开主页';
        chip.addEventListener('click', function() {
            window.open('https://space.bilibili.com/' + encodeURIComponent(this.dataset.upUid), '_blank', 'noopener');
        });
    }
    chip.addEventListener('mouseenter', function() {
        const upId = this.dataset.upid;
        if (upId && upWcData[upId] && upWcData[upId].length) {
            showUpWc(this, upWcData[upId]);
            return;
        }
        const upUid = this.dataset.upUid;
        if (!upUid) return;                         // 旧缓存画像无 uid：纯展示
        if (upWcCache[upUid]) { showUpWc(this, upWcCache[upUid]); return; }
        showUpWcText(this, '加载中…');
        const chip = this;
        fetchUpWc(upUid).then(words => {
            // 悬停已移开则不展示（用户只是路过）
            if (words) { if (chip.matches(':hover')) showUpWc(chip, words); }
            else if (chip.matches(':hover')) showUpWcText(chip, '该 UP 主暂无投稿数据或加载失败');
        });
    });
    chip.addEventListener('mouseleave', function() { popup.style.display = 'none'; });
});

// 高回复评论组：鼠标在组内悬停静止 600ms 弹出该组讨论主题词云（data-wc 词频由服务端注入）
let hotWcTimer = null;
document.querySelectorAll('.hot-item[data-wc]').forEach(item => {
    item.addEventListener('mousemove', function(e) {
        popup.style.display = 'none';   // 移动中不打扰，静止才弹
        clearTimeout(hotWcTimer);
        const raw = this.dataset.wc;
        hotWcTimer = setTimeout(() => {
            let data;
            try { data = JSON.parse(raw); } catch { return; }
            if (!data || !data.length) return;
            const maxW = Math.max(...data.map(d => d[1]));
            const minW = Math.min(...data.map(d => d[1]));
            // 基准字号 22：词少或权重相同时也要可读（旧基准 10 会缩成看不清的小字）
            const scaled = data.map(d => [d[0], 22 + (d[1] - minW) / Math.max(maxW - minW, 1) * 38]);
            WordCloud(popupCanvas, {list: scaled, gridSize: 10, weightFactor: 1, fontFamily: 'sans-serif',
                color: () => ['#00a1d6','#fb7299','#ff9f43','#6c5ce7','#2e7d32'][Math.floor(Math.random()*5)],
                rotateRatio: 0, backgroundColor: '#ffffff', shape: 'circle', clearCanvas: true});
            popup.style.left = Math.min(e.clientX + 12, window.innerWidth - 320) + 'px';
            popup.style.top = Math.min(e.clientY + 12, window.innerHeight - 260) + 'px';
            popup.style.display = 'block';
        }, 600);
    });
    item.addEventListener('mouseleave', function() {
        clearTimeout(hotWcTimer);
        popup.style.display = 'none';
    });
});

// ===== 争执焦点：关系图画布（多圆簇：受害者居中、攻击者环绕；同一人只画一个节点，
// 既攻击又被攻击的"链条"节点通过指向ta的边+ta发出的边体现） =====
// 共享交互状态：drawAfEdges 每次重画（切标签页/resize）只更新 af.* 引用，
// box 级（拖拽/缩放）与 window 级（节点拖动）监听只绑一次——此前每次重画都
// 重复 addEventListener 到持久的 box 与 window 上，造成监听器泄漏
const af = {box: null, content: null, st: null, edgeD: null, edgeRecs: [], rOf: null,
            nodeDrag: null, nlx: 0, nly: 0, dragging: false, moved: false, lx: 0, ly: 0,
            winBound: false};

function afApply() {
    if (af.content && af.st)
        af.content.setAttribute('transform', `translate(${af.st.x} ${af.st.y}) scale(${af.st.k})`);
}

function drawAfEdges() {
    const box = document.querySelector('.af-graph');
    if (!box) return;
    let g;
    try { g = JSON.parse(box.dataset.afGraph || '{}'); } catch { return; }
    box.innerHTML = '';
    if (!g.nodes || !g.links || !g.links.length) return;
    const W = box.clientWidth || 800;
    if (W === 0) return;   // 标签页未显示（宽度为0），等可见时再画
    const svgNS = 'http://www.w3.org/2000/svg';
    // 出入度：inDeg>0 的节点是受害者（可能同时是攻击者=链条节点），inDeg=0 是纯攻击者
    const inDeg = {}, outDeg = {};
    g.links.forEach(l => {
        inDeg[l.t] = (inDeg[l.t] || 0) + 1;
        outDeg[l.s] = (outDeg[l.s] || 0) + 1;
    });
    g.nodes.forEach(n => { n.n = (n.na || 0) + (n.nv || 0); });
    const maxN = Math.max(...g.nodes.map(n => n.n), 1);
    const maxW = Math.max(...g.links.map(l => l.w), 1);
    // 节点半径随总涉及次数线性放大（攻击/被攻击越多头像越大）
    const rOf = n => 9 + 21 * (n.n / maxN);
    // 每个发过攻击的节点一个颜色，其连出的边/箭头同色
    const PALETTE = ['#e53935', '#fb8c00', '#8e24aa', '#3949ab', '#00897b', '#7cb342',
                     '#c0ca33', '#6d4c41', '#d81b60', '#00acc1', '#5e35b1', '#f4511e',
                     '#43a047', '#1e88e5', '#757575', '#c2185b'];
    const aColor = {};
    const colorNodes = g.nodes.filter(n => n.na > 0);
    colorNodes.forEach((n, i) => { aColor[String(n.id)] = PALETTE[i % PALETTE.length]; });

    // 并查集求连通分量（按 uid 合并后的单节点图，边即攻击关系）
    const byId = {};
    g.nodes.forEach(n => { byId[String(n.id)] = n; });
    const parent = {};
    g.nodes.forEach(n => { parent[String(n.id)] = String(n.id); });
    const find = k => { while (parent[k] !== k) { parent[k] = parent[parent[k]]; k = parent[k]; } return k; };
    const linked = new Set();
    g.links.forEach(l => {
        const ks = String(l.s), kt = String(l.t);
        if (byId[ks] && byId[kt]) { parent[find(ks)] = find(kt); linked.add(ks); linked.add(kt); }
    });
    const comps = {};
    g.nodes.forEach(n => {
        const key = String(n.id);
        if (linked.has(key)) (comps[find(key)] = comps[find(key)] || []).push(n);
    });
    const compList = Object.values(comps).sort((a, b) =>
        b.reduce((s, n) => s + n.n, 0) - a.reduce((s, n) => s + n.n, 0));   // 大的分量先排

    // 多圆簇布局：每个连通分量一个独立小圆——受害者（含链条节点）居中、纯攻击者环绕；
    // 无边节点没有边要表达，用紧凑网格簇而不是大圆环（大环是纵向空间的最大浪费源）；
    // 簇按半径网格流式排布，大簇先放。边不出簇 → 天然零交叉；簇间网格隔离 → 不互相压
    const clusters = compList.map(comp => {
        const centers = comp.filter(n => (inDeg[n.id] || 0) > 0).sort((x, y) => y.n - x.n);
        const ring = comp.filter(n => (inDeg[n.id] || 0) === 0).sort((x, y) => y.n - x.n);
        return {centers, ring, w: comp.reduce((s, n) => s + n.n, 0)};
    });
    const freeNs = g.nodes.filter(n => !linked.has(String(n.id)));
    if (freeNs.length) clusters.push({centers: [], ring: [], free: freeNs, w: 0});
    clusters.forEach(c => {
        if (c.free) {
            // 无边节点网格簇：节点+标签为一格，行列近似方形
            const cellW = 120, cellH = 84;
            c.cols = Math.ceil(Math.sqrt(c.free.length));
            c.rows = Math.ceil(c.free.length / c.cols);
            c.halfW = c.cols * cellW / 2;
            c.halfH = c.rows * cellH / 2;
            c.rad = 0;
            return;
        }
        const ringN = c.ring.length;
        const maxRingR = ringN ? Math.max(...c.ring.map(rOf)) : 0;
        const maxCenterR = c.centers.length ? Math.max(...c.centers.map(rOf)) : 0;
        // 簇半径按节点实际尺寸计算（留白从宽，宁大勿小——边太短会看不清箭头指向）：
        // 环上相邻节点弦长 ≥ 直径和+34，环上节点与中心受害者 ≥ 半径和+56
        let rad = 110;
        if (ringN > 0) {
            rad = Math.max(rad, ringN * 26);
            if (ringN >= 2)
                rad = Math.max(rad, (maxRingR * 2 + 34) / (2 * Math.sin(Math.PI / ringN)));
            rad = Math.max(rad, maxCenterR + maxRingR + 56);
        }
        c.rad = rad;
        // 标签余量：切线旋转标签的最大延伸 = off(≈32) + 8字文本宽(≈95)，给足防簇间串字
        c.halfW = rad + 130;
        c.halfH = rad + 100;
    });
    clusters.sort((a, b) => b.halfW - a.halfW || b.w - a.w);
    // 虚拟布局宽度大于视口（横向排列，每行更多簇；画布可平移缩放，不必塞进一屏）
    const LW = W * 1.35;
    const GAP = 48, MARGIN = 30;
    let gx = MARGIN, gy = 0, rowH = 0, maxRowW = 0;
    clusters.forEach(c => {
        if (gx > MARGIN && gx + c.halfW * 2 > LW - MARGIN) {
            maxRowW = Math.max(maxRowW, gx - GAP);
            gx = MARGIN; gy += rowH + GAP; rowH = 0;
        }
        c.cx = gx + c.halfW; c.cy = gy + c.halfH;
        gx += c.halfW * 2 + GAP;
        rowH = Math.max(rowH, c.halfH * 2);
    });
    maxRowW = Math.max(maxRowW, gx - GAP);
    // 整体放大：内容没占满布局宽度时按比例放大簇中心与簇半径（节点大小不变、边拉长，
    // 避免大片空白下簇挤作一团），上限 1.8 倍
    const zoom = Math.min(1.8, (LW - MARGIN * 2) / Math.max(maxRowW, 1));
    if (zoom > 1)
        clusters.forEach(c => { c.cx *= zoom; c.cy *= zoom; c.rad *= zoom; });
    const H = (gy + rowH) * zoom + 26;
    // 视口结构：af-graph 是固定高度的可拖拽/缩放视口，全部内容挂在一个 <g> 变换组上
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', W);
    svg.setAttribute('height', '100%');
    const view = document.createElementNS(svgNS, 'g');
    svg.appendChild(view);
    const svgRoot = svg, content = view;   // 后续内容全部挂 content，svgRoot 仅作容器
    // 簇内定位：受害者（含链条节点）居中（多个并排），纯攻击者沿小圆环绕；无边簇网格排布
    clusters.forEach(c => {
        if (c.free) {
            // 网格簇：标签放节点正上方（center 同款 labUp=false 样式，用 grid 标记）
            c.free.forEach((n, i) => {
                n.grid = true;
                n.x = c.cx + (i % c.cols - (c.cols - 1) / 2) * 120;
                n.y = c.cy + (Math.floor(i / c.cols) - (c.rows - 1) / 2) * 84;
            });
            return;
        }
        // 中心受害者按实际直径累计放置（大头像不重叠，间距放宽至 20px）；标签上下交错
        // （节点间距≈直径和+20，但 8 字标签宽 ~85px，全放下方必撞）
        let vx = c.cx - (c.centers.reduce((s, n) => s + rOf(n) * 2, 0) + (c.centers.length - 1) * 20) / 2;
        c.centers.forEach((n, i) => {
            n.center = true;
            n.labUp = i % 2 === 1;
            n.x = vx + rOf(n);
            n.y = c.cy;
            vx += rOf(n) * 2 + 20;
        });
        c.ring.forEach((n, i) => {
            n.ang = -Math.PI / 2 + i * 2 * Math.PI / c.ring.length;
            n.x = c.cx + c.rad * Math.cos(n.ang);
            n.y = c.cy + c.rad * Math.sin(n.ang);
        });
    });

    // 箭头 marker 按攻击者颜色各建一个
    const defs = document.createElementNS(svgNS, 'defs');
    const markerIds = {};
    const markerFor = color => {
        if (!markerIds[color]) {
            const id = 'af-arr-' + color.slice(1);
            const m = document.createElementNS(svgNS, 'marker');
            m.setAttribute('id', id);
            m.setAttribute('viewBox', '0 0 10 10');
            m.setAttribute('refX', '9'); m.setAttribute('refY', '5');
            m.setAttribute('markerWidth', '8'); m.setAttribute('markerHeight', '8');
            m.setAttribute('orient', 'auto');
            const tri = document.createElementNS(svgNS, 'path');
            tri.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
            tri.setAttribute('fill', color);
            m.appendChild(tri);
            defs.appendChild(m);
            markerIds[color] = id;
        }
        return markerIds[color];
    };
    content.appendChild(defs);

    // 边：攻击者→受害者带箭头曲线（链条节点的出边即从中心连向另一个中心）；
    // 指向同一受害者的平行边交替向两侧弯曲，减少重叠。edgeRecs 记录每条边的端点与弯曲量，
    // 节点被拖动时按此重算路径
    const edgeD = (a, v, off) => {
        const dx = v.x - a.x, dy = v.y - a.y;
        const len = Math.hypot(dx, dy) || 1;
        const ux = dx / len, uy = dy / len;
        // 起点=攻击者节点中心（被节点盖住不露端头）；终点=受害者节点边缘退箭头位，
        // 退缩量按边长自适应——短边若全额退缩只剩 1px 线段，会变成节点旁的小疙瘩
        const sx = a.x, sy = a.y;
        const inset = Math.min(rOf(v) + 8, len * 0.4);
        const ex = v.x - ux * inset, ey = v.y - uy * inset;
        const qx = (sx + ex) / 2 - uy * off, qy = (sy + ey) / 2 + ux * off;
        return `M ${sx} ${sy} Q ${qx} ${qy} ${ex} ${ey}`;
    };
    const edgeRecs = [];
    const perTarget = {};
    g.links.forEach(l => {
        const a = byId[String(l.s)], v = byId[String(l.t)];
        if (!a || !v) return;
        const idx = (perTarget[l.t] = (perTarget[l.t] || 0) + 1) - 1;
        const color = aColor[String(l.s)] || '#e53935';
        const off = (idx % 2 ? 1 : -1) * Math.ceil(idx / 2) * 14   // 交替两侧、逐条加宽
                  * Math.min(1, Math.hypot(v.x - a.x, v.y - a.y) / 120);  // 短边少弯，防弯成钩子
        const p = document.createElementNS(svgNS, 'path');
        p.setAttribute('d', edgeD(a, v, off));
        p.setAttribute('class', 'af-edge');
        p.style.stroke = color;
        p.style.strokeWidth = (1 + 3 * l.w / maxW).toFixed(1);
        p.setAttribute('marker-end', `url(#${markerFor(color)})`);
        p.dataset.uids = `${l.s},${l.t}`;
        content.appendChild(p);
        edgeRecs.push({p, a, v, off});
    });

    // 节点：头像+名字×次数；居中受害者标签放正上/下方，环上标签沿切线旋转
    // 节点拖动状态存共享 af（window 级 pointermove 监听只绑一次，见函数尾部）
    g.nodes.forEach(n => {
        const gEl = document.createElementNS(svgNS, 'g');
        gEl.setAttribute('class', 'af-node');
        const c = document.createElementNS(svgNS, 'circle');
        c.setAttribute('cx', n.x); c.setAttribute('cy', n.y);
        c.setAttribute('r', rOf(n).toFixed(1));
        c.setAttribute('class', n.na > 0 ? 'af-node-a' : 'af-node-v');
        if (n.na > 0) c.style.fill = aColor[String(n.id)];   // 攻击者节点与边同色
        const t = document.createElementNS(svgNS, 'text');
        const maxLen = 8;   // 标签统一截 8 字，配合簇半径余量防越界
        const nm = String(n.name || '匿名');   // users.name 可能为 NULL → 防 TypeError 白屏
        const label = nm.length > maxLen ? nm.slice(0, maxLen) + '…' : nm;
        // 计数文本：双角色节点（链条）显示 攻×na 被×nv，单角色显示 ×n
        const cnt = (n.na > 0 && n.nv > 0) ? `攻${n.na} 被${n.nv}` : `×${n.n}`;
        const off = rOf(n) + 8;
        let tx, ty, anchor;
        if (n.center || n.grid) {
            // 中心受害者上下交错防相邻标签相撞；无边网格节点统一放正上方
            tx = n.x;
            ty = n.y + ((n.labUp || n.grid) ? -(rOf(n) + 14) : rOf(n) + 14);
            anchor = 'middle';
        } else {
            const cos = Math.cos(n.ang), sin = Math.sin(n.ang);
            // 切线旋转：上半环 +90°，下半环 -90° 且锚点翻转为 end（判定用 sin，用 cos 会倒挂）
            const bottom = sin > 0;
            const deg = n.ang * 180 / Math.PI + (bottom ? -90 : 90);
            tx = n.x + cos * off;
            ty = n.y + sin * off + 4;
            anchor = bottom ? 'end' : 'start';
            t.setAttribute('transform',
                `rotate(${deg.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)})`);
        }
        t.setAttribute('x', tx);
        t.setAttribute('y', ty);
        t.setAttribute('text-anchor', anchor);
        t.setAttribute('class', n.na > 0 ? 'af-lab' : 'af-lab af-lab-v');
        t.textContent = `${label} ${cnt}`;
        gEl.appendChild(c);
        // 头像节点：有 face 的用圆形裁剪头像盖在色点上（加载失败移除图片，回退为色点）
        if (n.face) {
            const img = document.createElementNS(svgNS, 'image');
            const ir = rOf(n) - 1.5;
            img.setAttribute('x', n.x - ir); img.setAttribute('y', n.y - ir);
            img.setAttribute('width', 2 * ir); img.setAttribute('height', 2 * ir);
            img.setAttribute('href', n.face);
            img.setAttribute('referrerpolicy', 'no-referrer');
            img.style.clipPath = 'circle(50% at 50% 50%)';
            img.addEventListener('error', () => img.remove());
            gEl.appendChild(img);
        }
        gEl.appendChild(t);
        // 节点单独拖动：按下节点只拖节点（不动画布），边随动重算；拖动后吞掉 click
        gEl.addEventListener('pointerdown', e => {
            e.stopPropagation();
            af.nodeDrag = {n, c, img: gEl.querySelector('image'), t,
                           tx, ty, deg: t.getAttribute('transform') || ''};
            af.nlx = e.clientX; af.nly = e.clientY;
        });
        gEl.addEventListener('mouseenter', () => {
            // 单节点：高亮与 ta 相关的全部边（发出的+指向的，链条节点两侧关系一起亮）
            svg.querySelectorAll('.af-edge').forEach(p =>
                p.classList.toggle('af-edge-hot',
                    (p.dataset.uids || '').split(',').includes(String(n.id))));
        });
        gEl.addEventListener('mouseleave', () =>
            svg.querySelectorAll('.af-edge.af-edge-hot').forEach(p => p.classList.remove('af-edge-hot')));
        gEl.addEventListener('click', () => {
            // 定位明细条目（挑事者/被围攻者两个列表都可能有 ta，取第一个命中）
            const item = document.querySelector(`.af-item[data-uid="${CSS.escape(String(n.id))}"]`);
            if (item) {
                item.scrollIntoView({behavior: 'smooth', block: 'center'});
                item.classList.add('af-flash');
                setTimeout(() => item.classList.remove('af-flash'), 1200);
            }
        });
        content.appendChild(gEl);
    });
    box.appendChild(svg);

    // 视口交互：拖动平移 + 滚轮缩放（以光标为中心）。
    // 默认缩放：内容恰好撑满视口宽度（k = W/LW ≈ 0.74），顶对齐；内容矮时垂直居中
    const vh = box.clientHeight || 520;
    af.st = {x: 0, y: 16, k: Math.min(1, W / LW)};
    af.st.x = (W - LW * af.st.k) / 2;   // 水平居中（k<1 时内容恰满宽，x≈0）
    if (H * af.st.k < vh) af.st.y = (vh - H * af.st.k) / 2;
    // 重画只更新共享引用，监听器回调全部读写 af.*（下方 if 守卫保证只绑一次）
    af.content = content;
    af.edgeD = edgeD;
    af.edgeRecs = edgeRecs;
    af.rOf = rOf;
    afApply();
    if (af.box !== box) {
        // box 是服务端渲染的持久元素（重画只清空内部 SVG）：监听只绑一次
        af.box = box;
        box.addEventListener('pointerdown', e => {
            af.dragging = true; af.moved = false;
            af.lx = e.clientX; af.ly = e.clientY;
            box.setPointerCapture(e.pointerId);
        });
        box.addEventListener('pointermove', e => {
            if (!af.dragging || !af.st) return;
            const dx = e.clientX - af.lx, dy = e.clientY - af.ly;
            if (Math.abs(dx) + Math.abs(dy) > 3) af.moved = true;
            af.lx = e.clientX; af.ly = e.clientY;
            af.st.x += dx; af.st.y += dy;
            afApply();
        });
        box.addEventListener('pointerup', () => { af.dragging = false; });
        box.addEventListener('wheel', e => {
            if (!af.st) return;
            e.preventDefault();
            const rect = box.getBoundingClientRect();
            const px = e.clientX - rect.left, py = e.clientY - rect.top;
            const k2 = Math.min(4, Math.max(0.2, af.st.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
            // 以光标为中心缩放：光标下的内容点保持不动
            af.st.x = px - (px - af.st.x) * (k2 / af.st.k);
            af.st.y = py - (py - af.st.y) * (k2 / af.st.k);
            af.st.k = k2;
            afApply();
        }, {passive: false});
        // 拖拽后的抬起会紧跟一次 click：吞掉它，避免误触节点的"定位到明细"
        box.addEventListener('click', e => {
            if (af.moved) { e.stopPropagation(); e.preventDefault(); af.moved = false; }
        }, true);
    }
    if (!af.winBound) {
        // 节点拖动：屏幕位移 ÷ 缩放系数换算回内容坐标；相连的边随动重算
        af.winBound = true;
        window.addEventListener('pointermove', e => {
            if (!af.nodeDrag || !af.st) return;
            const dx = (e.clientX - af.nlx) / af.st.k, dy = (e.clientY - af.nly) / af.st.k;
            af.nlx = e.clientX; af.nly = e.clientY;
            if (Math.abs(dx) + Math.abs(dy) > 0.5) af.moved = true;   // 触发 click 吞没
            const n = af.nodeDrag.n;
            n.x += dx; n.y += dy;
            af.nodeDrag.c.setAttribute('cx', n.x);
            af.nodeDrag.c.setAttribute('cy', n.y);
            if (af.nodeDrag.img) {
                af.nodeDrag.img.setAttribute('x', n.x - (af.rOf(n) - 1.5));
                af.nodeDrag.img.setAttribute('y', n.y - (af.rOf(n) - 1.5));
            }
            af.nodeDrag.tx += dx; af.nodeDrag.ty += dy;
            af.nodeDrag.t.setAttribute('x', af.nodeDrag.tx);
            af.nodeDrag.t.setAttribute('y', af.nodeDrag.ty);
            if (af.nodeDrag.deg)   // 旋转标签的旋转中心跟随
                af.nodeDrag.t.setAttribute('transform',
                    af.nodeDrag.deg.replace(/rotate\((\S+) \S+ \S+\)/,
                        (m, d) => `rotate(${d} ${af.nodeDrag.tx.toFixed(1)} ${af.nodeDrag.ty.toFixed(1)})`));
            af.edgeRecs.forEach(r => {
                if (r.a === n || r.v === n) r.p.setAttribute('d', af.edgeD(r.a, r.v, r.off));
            });
        });
        window.addEventListener('pointerup', () => { af.nodeDrag = null; });
    }
    // 明细列表的挑事者条目前缀同色圆点：图与列表颜色互参
    colorNodes.forEach(n => {
        const item = document.querySelector(`.af-item[data-side="a"][data-uid="${CSS.escape(String(n.id))}"]`);
        const line = item && item.querySelector('.af-line');
        if (line && !line.querySelector('.af-dot')) {
            const dot = document.createElement('span');
            dot.className = 'af-dot';
            dot.style.background = aColor[String(n.id)];
            line.prepend(dot);
        }
    });
}

window.addEventListener('resize', () => {
    if (document.querySelector('#tab-attack.active')) drawAfEdges();
});

// ===== 用户画像：筛选 + 搜索防抖 + 排序 + 前端分页（全部读卡片 data-* 属性，不再做 DOM 位置解析） =====
const USER_PAGE_SIZE = 24;
const userState = {filter: 'all', kw: '', sort: 'risk', page: 1};
let userSearchTimer = null;
let userCards = [];   // 缓存卡片元素与解析后的数据，避免每次 querySelectorAll 重读 DOM

function userCardData(card) {
    return {
        el: card,
        level: parseInt(card.dataset.level) || 0,
        vip: card.dataset.vip === 'true',
        spam: card.dataset.spam || '低',
        official: card.dataset.official === 'true',
        isUp: card.dataset.isUp === 'true',
        spamScore: parseFloat(card.dataset.spamScore) || 0,
        dmCount: parseInt(card.dataset.danmakuCount) || 0,
        fans: parseInt(card.dataset.fans) || 0,
        name: (card.querySelector('.username')?.textContent || '').toLowerCase(),
        uid: (card.querySelector('.uid')?.textContent || '').toLowerCase(),
        riskIdx: 0,   // 服务端 sort_profiles_by_risk 顺序下标，initUserCards 中赋值
    };
}

function initUserCards() {
    const grid = document.getElementById('userGrid');
    if (!grid) return;
    userCards = Array.from(grid.querySelectorAll('.user-card')).map((el, i) => {
        const d = userCardData(el);
        d.riskIdx = i;
        return d;
    });
    // 昵称/UID 搜索：300ms 防抖
    document.getElementById('userSearch').addEventListener('input', () => {
        clearTimeout(userSearchTimer);
        userSearchTimer = setTimeout(() => {
            userState.kw = document.getElementById('userSearch').value.trim().toLowerCase();
            userState.page = 1;
            renderUserCards();
        }, 300);
    });
    document.getElementById('userSort').addEventListener('change', function() {
        userState.sort = this.value;
        userState.page = 1;
        renderUserCards();
    });
    renderUserCards();
}

function userFilter(type, btn) {
    userState.filter = type;
    userState.page = 1;
    document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderUserCards();
}

function userMatchFilter(d) {
    switch (userState.filter) {
        case 'all': return true;
        case 'high-level': return d.level >= 5;
        case 'vip': return d.vip;
        case 'official': return d.official;
        case 'spam': return d.spam !== '低';
        case 'creator': return d.isUp;
    }
    return true;
}

function userSortCmp(a, b) {
    switch (userState.sort) {
        case 'spam-score': return b.spamScore - a.spamScore || a.riskIdx - b.riskIdx;
        case 'danmaku': return b.dmCount - a.dmCount || a.riskIdx - b.riskIdx;
        case 'fans': return b.fans - a.fans || a.riskIdx - b.riskIdx;
        default: return a.riskIdx - b.riskIdx;   // risk：保持服务端风险序
    }
}

function renderUserCards() {
    const grid = document.getElementById('userGrid');
    if (!grid) return;
    const hit = userCards.filter(d => userMatchFilter(d) &&
        (!userState.kw || d.name.includes(userState.kw) || d.uid.includes(userState.kw)));
    hit.sort(userSortCmp);
    hit.forEach(d => grid.appendChild(d.el));   // appendChild 移动已挂载节点完成重排
    const pages = Math.max(1, Math.ceil(hit.length / USER_PAGE_SIZE));
    userState.page = Math.min(userState.page, pages);
    const start = (userState.page - 1) * USER_PAGE_SIZE;
    const showSet = new Set(hit.slice(start, start + USER_PAGE_SIZE).map(d => d.el));
    userCards.forEach(d => { d.el.style.display = showSet.has(d.el) ? '' : 'none'; });
    document.getElementById('userResultCount').textContent =
        '共 ' + userCards.length + ' 人 · 命中 ' + hit.length + ' 人';
    document.getElementById('userEmpty').style.display = (userCards.length && !hit.length) ? 'block' : 'none';
    renderUserPager(pages);
}

function renderUserPager(pages) {
    const pager = document.getElementById('userPager');
    if (pages <= 1) { pager.innerHTML = ''; return; }
    let html = '<button class="pager-btn" ' + (userState.page <= 1 ? 'disabled' : '') +
        ' onclick="userPage(' + (userState.page - 1) + ')">上一页</button>';
    for (let p = 1; p <= pages; p++) {
        html += '<button class="pager-btn' + (p === userState.page ? ' active' : '') +
            '" onclick="userPage(' + p + ')">' + p + '</button>';
    }
    html += '<button class="pager-btn" ' + (userState.page >= pages ? 'disabled' : '') +
        ' onclick="userPage(' + (userState.page + 1) + ')">下一页</button>';
    pager.innerHTML = html;
}

function userPage(p) {
    userState.page = p;
    renderUserCards();
    // 翻页后自动回到画像区顶部（fix：避免停在上次滚动位置，看不到新页开头）
    const bar = document.querySelector('#tab-users .filter-bar');
    (bar || document.getElementById('userGrid')).scrollIntoView({block: 'start'});
}

// 弹幕浏览器点击发送者跳转到用户画像卡片（锚点 id="uid-{uid}"，spec 4）
// 分页感知：重置筛选/搜索后翻到目标卡片所在页，再滚动高亮
function gotoUser(uid) {
    switchTab('users');
    clearTimeout(userSearchTimer);   // 清掉滞后的搜索防抖回调，防止抵消下面的翻页
    const el = document.getElementById('uid-' + uid);
    if (!el) return;
    userState.filter = 'all';
    userState.kw = '';
    const searchEl = document.getElementById('userSearch');
    if (searchEl) searchEl.value = '';
    document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.filter === 'all'));
    const d = userCards.find(c => c.el === el);
    if (d) {
        const hit = userCards.filter(x => userMatchFilter(x));
        hit.sort(userSortCmp);
        userState.page = Math.max(1, Math.floor(hit.indexOf(d) / USER_PAGE_SIZE) + 1);
        renderUserCards();
    }
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.style.boxShadow = '0 0 0 3px #00a1d6';
    setTimeout(() => { el.style.boxShadow = ''; }, 2000);
}

// 弹幕浏览器（JSON API + 前端渲染当前页，spec 4）
const BVID = PAGE_DATA.bvid;
const dmState = {page: 1};
let dmTimer = null;
let dmReqSeq = 0;   // 弹幕请求自增序号：防抖后仍可能并发多个 in-flight 请求，旧响应后到不得覆盖新响应

function dmParams() {
    const p = new URLSearchParams();
    const search = document.getElementById('dmSearch').value.trim();
    const sender = document.getElementById('dmSender').value.trim();
    const cat = document.getElementById('dmCategory').value;
    const spam = document.getElementById('dmSpam').value;
    if (search) p.set('search', search);
    if (sender) p.set('sender', sender);
    if (cat) p.set('category', cat);
    if (spam) p.set('spam', spam);
    if (document.getElementById('dmAnalyzed').checked) p.set('analyzed', '1');
    p.set('sort', document.getElementById('dmSort').value);
    p.set('order', document.getElementById('dmOrder').value);
    p.set('page_size', document.getElementById('dmPageSize').value);
    p.set('page', dmState.page);
    return p.toString();
}

// 弹幕浏览器状态写入 query 参数（spec 5）：搜索词/发送者/筛选/排序/页码/每页条数
function dmSyncUrl() {
    const u = new URL(location.href);
    ['search', 'sender', 'category', 'spam', 'sort', 'order', 'page', 'page_size', 'analyzed']
        .forEach(k => u.searchParams.delete(k));
    new URLSearchParams(dmParams()).forEach((v, k) => u.searchParams.set(k, v));
    history.replaceState(null, '', u.toString());
}

// 页面加载时从 query 参数还原弹幕浏览器控件与页码
function dmRestoreFromUrl() {
    const q = new URLSearchParams(location.search);
    const setVal = (id, key) => { if (q.has(key)) document.getElementById(id).value = q.get(key); };
    setVal('dmSearch', 'search');
    setVal('dmSender', 'sender');
    setVal('dmCategory', 'category');
    setVal('dmSpam', 'spam');
    setVal('dmSort', 'sort');
    setVal('dmOrder', 'order');
    setVal('dmPageSize', 'page_size');
    if (q.get('analyzed') === '1') document.getElementById('dmAnalyzed').checked = true;
    const pg = parseInt(q.get('page'));
    if (!isNaN(pg) && pg >= 1) dmState.page = pg;
}

function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtVideoTime(sec) {
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function loadDanmaku() {
    const seq = ++dmReqSeq;   // 本次请求序号：响应回来若已有更新请求发出则丢弃，防止旧响应覆盖新表格
    const err = document.getElementById('dmError');
    const spinner = document.getElementById('dmSpinner');
    err.style.display = 'none';
    spinner.style.display = 'block';
    fetch('/api/video/' + encodeURIComponent(BVID) + '/danmaku?' + dmParams())
        .then(r => {
            if (!r.ok) return r.json().then(j => Promise.reject(new Error(j.error || ('HTTP ' + r.status))));
            return r.json();
        })
        .then(data => {
            if (seq !== dmReqSeq) return;   // 过期响应：已有更新的请求在途，不渲染避免表格与 URL/筛选不一致
            const tbody = document.getElementById('dmTbody');
            tbody.innerHTML = data.rows.map(row => {
                // uid 只接受整数（服务端已归一为 int）：非整数一律不渲染跳转链接，
                // 避免被污染的值突破内联 JS 上下文（纵深防御）
                const uidNum = Number(row.uid);
                const sender = Number.isInteger(uidNum)
                    ? '<a onclick="gotoUser(' + uidNum + ')">' + escHtml(row.name || uidNum) + '</a><br><span class="dm-time">UID:' + uidNum + '</span>'
                    : (row.name
                        // 遮蔽态（uid 被服务端遮蔽为非整数字符串）：不渲染跳转链接，但遮蔽昵称 row.name 仍展示（过 escHtml，防御不放松）
                        ? escHtml(row.name) + '<br><span class="dm-time">' + escHtml(row.mid_hash) + '</span>'
                        : '<span class="dm-time">' + escHtml(row.mid_hash) + '</span>');
                const dup = row.dup_count > 1 ? ' <span class="dm-time">×' + row.dup_count + '</span>' : '';
                // 颜色只接受 #rrggbb（服务端已归一）：否则不渲染色块，防 CSS 声明注入
                const safeColor = /^#[0-9a-fA-F]{6}$/.test(row.color || '') ? row.color : '';
                const dot = safeColor ? '<span class="dm-dot" style="background:' + safeColor +
                    '" title="' + escHtml(safeColor) + '"></span>' : '';
                const cats = (row.categories || []).map(c =>
                    '<span style="display:inline-block;background:' + (dmCatColors[c] || '#999') +
                    ';color:#fff;font-size:12px;border-radius:4px;padding:1px 8px;margin:1px 2px;">' +
                    escHtml(c) + '</span>').join('');
                const chk = '<input type="checkbox" class="dm-check" data-mid="' + escHtml(row.mid_hash) + '"' +
                    (dmSelected.has(row.mid_hash) ? ' checked' : '') + '>';
                // 多分P 视频：该弹幕首次出现的分P 必须标出（time 是分P 内相对秒数）
                const vt = (data.multi_page && row.first_page
                    ? 'P' + row.first_page + ' ' : '') + fmtVideoTime(row.first_video_time);
                return '<tr><td>' + chk + '</td><td>' + dot + escHtml(row.content) + dup + '</td><td>' + sender + '</td><td>' +
                    vt + '</td><td>' +
                    new Date(row.first_send_time * 1000).toLocaleString() + '</td><td>' + cats + '</td><td>' +
                    escHtml(row.spam_level) + '</td></tr>';
            }).join('') || '<tr><td colspan="7" class="empty-note">无匹配弹幕</td></tr>';
            dmBindChecks();
            const pageSize = data.page_size || 100;
            const pages = Math.max(1, Math.ceil(data.total / pageSize));
            document.getElementById('dmPageInfo').textContent =
                '第 ' + data.page + ' / ' + pages + ' 页（共 ' + data.total + ' 行）';
            document.getElementById('dmPrev').disabled = data.page <= 1;
            document.getElementById('dmNext').disabled = data.page >= pages;
            dmSyncUrl();
        })
        .catch(e => {
            if (seq !== dmReqSeq) return;   // 过期请求的失败不展示（当前在途的是更新请求）
            document.getElementById('dmErrorText').textContent = '弹幕加载失败: ' + e.message;
            err.style.display = 'flex';
        })
        .finally(() => { if (seq === dmReqSeq) spinner.style.display = 'none'; });   // 过期请求不得提前关掉新请求的 spinner
}

// 页码输入跳转（spec 3）
function dmGotoPage() {
    const v = parseInt(document.getElementById('dmGoto').value);
    if (!isNaN(v) && v >= 1) {
        dmState.page = v;
        loadDanmaku();
    }
}

// 每页条数切换（50/100/200）：回到第 1 页
function dmPageSizeChange() {
    dmState.page = 1;
    loadDanmaku();
}

function dmPage(delta) { dmState.page = Math.max(1, dmState.page + delta); loadDanmaku(); }
function dmReload() { dmState.page = 1; loadDanmaku(); }

// 统计面板 Top10 点击 → 切到弹幕浏览器并筛选该发送者（spec 4）
function filterSender(midHash) {
    switchTab('danmaku');
    document.getElementById('dmSender').value = midHash;
    dmReload();
}

// 用户画像初始化（筛选/排序/分页/搜索防抖）
initUserCards();

// 事件绑定（旧视频无全量弹幕时无 dmTbody，跳过）
if (document.getElementById('dmTbody')) {
    document.getElementById('dmSearch').addEventListener('input', () => { clearTimeout(dmTimer); dmTimer = setTimeout(dmReload, 400); });
    document.getElementById('dmSender').addEventListener('input', () => { clearTimeout(dmTimer); dmTimer = setTimeout(dmReload, 400); });
    ['dmCategory', 'dmSpam', 'dmSort', 'dmOrder', 'dmAnalyzed'].forEach(id =>
        document.getElementById(id).addEventListener('change', dmReload));
    document.getElementById('dmPageSize').addEventListener('change', dmPageSizeChange);
    document.getElementById('dmGoto').addEventListener('keydown', e => { if (e.key === 'Enter') dmGotoPage(); });
    document.getElementById('dmCheckAll').addEventListener('change', function() {
        document.querySelectorAll('.dm-check').forEach(c => {
            c.checked = this.checked;
            if (this.checked) dmSelected.add(c.dataset.mid); else dmSelected.delete(c.dataset.mid);
        });
        dmUpdateAnalyzeStatus();
    });
    dmRestoreFromUrl();
    loadDanmaku();
}

// 手动勾选分析（spec B）：勾选状态跨页保留在 dmSelected（key=mid_hash，天然去重）；
// job_id 存 sessionStorage，刷新页面后可继续轮询（spec 5；服务重启 job 丢失则提示后清除）
const dmSelected = new Set();
const DM_JOB_KEY = 'dmJob_' + BVID;

function dmUpdateAnalyzeStatus(text) {
    const el = document.getElementById('dmAnalyzeStatus');
    if (!el) return;  // 旧视频无弹幕面板时无此元素
    el.textContent = text !== undefined ? text
        : (dmSelected.size ? '已选 ' + dmSelected.size + ' 个发送者' : '');
}
function dmBindChecks() {
    document.querySelectorAll('.dm-check').forEach(c =>
        c.addEventListener('change', function() {
            if (this.checked) dmSelected.add(this.dataset.mid); else dmSelected.delete(this.dataset.mid);
            dmUpdateAnalyzeStatus();
        }));
}
function startAnalysis() {
    const mids = Array.from(dmSelected);
    if (!mids.length) { dmUpdateAnalyzeStatus('请先勾选弹幕行'); return; }
    document.getElementById('dmFailBar').style.display = 'none';   // 新 job 启动时收起旧失败条
    dmUpdateAnalyzeStatus('正在启动分析...');
    fetch('/api/video/' + encodeURIComponent(BVID) + '/analyze', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mid_hashes: mids})
    }).then(r => r.json().then(j => ({ok: r.ok, j})))
      .then(({ok, j}) => {
          if (!ok) { dmUpdateAnalyzeStatus('启动失败: ' + (j.error || ('HTTP 未知错误'))); return; }
          sessionStorage.setItem(DM_JOB_KEY, j.job_id);
          // job 轮询期间禁用发起按钮：DM_JOB_KEY 单槽，防止并发 job 互相覆盖跟踪状态
          document.getElementById('dmAnalyzeBtn').disabled = true;
          dmSelected.clear();
          document.querySelectorAll('.dm-check').forEach(c => c.checked = false);
          pollJob(j.job_id);
      })
      .catch(e => dmUpdateAnalyzeStatus('启动失败: ' + e.message));   // 发起失败按钮保持可用
}
let jobPollFails = 0;   // 连续轮询失败计数：>=5 判定状态接口不可用，停止轮询并提示（spec 错误处理）
function pollJob(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            jobPollFails = 0;
            if (j.error) {  // 服务重启 job 丢失
                dmUpdateAnalyzeStatus('任务状态查询失败: ' + j.error + '（数据可能已部分落库）');
                sessionStorage.removeItem(DM_JOB_KEY);
                document.getElementById('dmAnalyzeBtn').disabled = false;
                return;
            }
            dmUpdateAnalyzeStatus('分析中 ' + j.done + '/' + j.total + (j.current ? '　' + j.current : ''));
            if (j.finished) {
                sessionStorage.removeItem(DM_JOB_KEY);
                document.getElementById('dmAnalyzeBtn').disabled = false;
                const errs = j.errors || [];
                // 新卡片需服务端渲染才有 DOM：记录待高亮 UID 与结果文案，重载后由加载恢复逻辑展示
                sessionStorage.setItem('dmFlash_' + BVID, JSON.stringify(j.results || []));
                sessionStorage.setItem('dmFlashMsg_' + BVID,
                    '手动分析完成: 成功 ' + (j.results || []).length + '/' + j.total +
                    (errs.length ? '（' + errs.length + ' 条失败）' : ''));
                if (errs.length) {
                    // 有失败项：不自动重载，展示失败明细条，由用户选「重试失败项」或「刷新查看结果」
                    renderJobFailures(errs);
                } else {
                    saveViewState();
                    location.reload();
                }
            } else {
                setTimeout(() => pollJob(jobId), 2000);
            }
        })
        .catch(() => {
            jobPollFails++;
            if (jobPollFails >= 5) {
                dmUpdateAnalyzeStatus('进度查询失败，请稍后刷新页面（job 可能仍在后台运行）');
                document.getElementById('dmAnalyzeBtn').disabled = false;
                return;   // job_id 保留在 sessionStorage，刷新后恢复轮询
            }
            setTimeout(() => pollJob(jobId), 2000);
        });
}

// 失败明细条：列出失败 mid_hash 与错误摘要；「重试失败项」把失败集合塞回 dmSelected 复用 startAnalysis
function renderJobFailures(errors) {
    const bar = document.getElementById('dmFailBar');
    document.getElementById('dmFailList').innerHTML = errors.map(e =>
        '<li><code>' + escHtml(e.mid_hash || '-') + '</code>：' + escHtml(e.error) + '</li>').join('');
    bar.style.display = 'block';
    dmUpdateAnalyzeStatus('分析完成，' + errors.length + ' 项失败');
    document.getElementById('dmRetryBtn').onclick = function() {
        const mids = errors.map(e => e.mid_hash).filter(Boolean);
        if (!mids.length) {   // 整体性失败（如登录态失效的全局终止）无 mid_hash，无单项可重试
            dmUpdateAnalyzeStatus('失败为整体性错误（如登录态失效），无单项可重试——请处理后重新勾选分析');
            bar.style.display = 'none';
            return;
        }
        bar.style.display = 'none';
        dmSelected.clear();
        mids.forEach(m => dmSelected.add(m));
        startAnalysis();
    };
    document.getElementById('dmReloadBtn').onclick = function() {
        saveViewState();
        location.reload();
    };
}

// 现场保存/恢复（sessionStorage，仅本次会话）：job 完成重载页面后回到原标签页/筛选/排序/分页/滚动位置
function saveViewState() {
    const state = {
        tab: document.querySelector('.tab-btn.active')?.dataset.tab || 'overview',
        userFilter: userState.filter,
        userKw: document.getElementById('userSearch')?.value || '',
        userSort: userState.sort,
        userPage: userState.page,
        scrollY: window.scrollY,
    };
    sessionStorage.setItem('viewState_' + BVID, JSON.stringify(state));
}

function restoreViewState() {
    const raw = sessionStorage.getItem('viewState_' + BVID);
    if (!raw) return false;
    sessionStorage.removeItem('viewState_' + BVID);   // 一次性消费
    try {
        const s = JSON.parse(raw);
        userState.filter = s.userFilter || 'all';
        userState.kw = (s.userKw || '').trim().toLowerCase();
        userState.sort = s.userSort || 'risk';
        userState.page = s.userPage || 1;
        const searchEl = document.getElementById('userSearch');
        if (searchEl) searchEl.value = s.userKw || '';
        const sortEl = document.getElementById('userSort');
        if (sortEl) sortEl.value = userState.sort;
        document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.filter === userState.filter));
        renderUserCards();
        switchTab(s.tab || 'overview');
        window.scrollTo(0, s.scrollY || 0);
        return true;
    } catch (e) { return false; }
}

// 页面加载恢复：优先处理"分析完成重载"的现场恢复+结果提示+高亮；否则恢复手动刷新的现场；
// 再否则有未完成 job 则继续轮询
(function() {
    const flashMsg = sessionStorage.getItem('dmFlashMsg_' + BVID);
    if (flashMsg) {
        sessionStorage.removeItem('dmFlashMsg_' + BVID);
        // 损坏数据回退 []（防 sessionStorage 内容异常导致整段恢复逻辑中断）
        let uids = [];
        try { uids = JSON.parse(sessionStorage.getItem('dmFlash_' + BVID) || '[]'); } catch { uids = []; }
        if (!Array.isArray(uids)) uids = [];
        sessionStorage.removeItem('dmFlash_' + BVID);
        restoreViewState();            // 恢复重载前的标签页/筛选/排序/分页/滚动
        switchTab('users');
        dmUpdateAnalyzeStatus(flashMsg);
        uids.forEach(uid => {
            const el = document.getElementById('uid-' + uid);
            if (el) {
                el.classList.add('flash-highlight');
                setTimeout(() => el.classList.remove('flash-highlight'), 3000);
            }
        });
        // gotoUser 内部会把筛选重置为"全部"并翻到目标卡片所在页——新卡片可见优先于现场筛选
        if (uids.length) gotoUser(uids[0]);
        return;
    }
    if (restoreViewState()) return;    // 「刷新查看结果」按钮的手动重载现场
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) {
        // 恢复未完成 job 的轮询：同样禁用发起按钮，防止并发 job 覆盖 DM_JOB_KEY
        document.getElementById('dmAnalyzeBtn').disabled = true;
        pollJob(jobId);
    }
    restoreTabFromHash();              // 前面分支未 return 时按 URL hash 还原标签页（job 续轮为异步轮询，不冲突）
})();

// ===== 报告页：重新生成 / 删除（spec 9） =====
// 隐藏信息开关（Cookie mask=1，全站共享）：开/关切换后整页刷新，服务端按遮蔽态重渲染
function toggleMask() {
    if (document.cookie.split('; ').includes('mask=1'))
        document.cookie = 'mask=; Max-Age=0; path=/';
    else
        document.cookie = 'mask=1; path=/';
    location.reload();
}

// B站屏蔽列表导出：读面板勾选标准/上限 → fetch 生成 → blob 下载；
// 无命中/服务端错误时 alert 提示而非下载空文件（uid 参数走 encodeURIComponent 防注入）
function blkExport(bvid) {
    const crit = [
        document.getElementById('blkCringe').checked ? 'cringe' : '',
        document.getElementById('blkSpam').checked ? 'spam' : '',
        document.getElementById('blkCmt').checked ? 'cmt' : '',
    ].filter(Boolean);
    if (!crit.length) { alert('请至少勾选一项导出标准'); return; }
    const max = parseInt(document.getElementById('blkMax').value, 10) || 200;
    const lowconf = document.getElementById('blkLowconf').checked ? '&lowconf=1' : '';
    const url = '/api/video/' + encodeURIComponent(bvid) +
        '/blocklist?crit=' + encodeURIComponent(crit.join(',')) + '&max=' + max + lowconf;
    fetch(url)
        .then(r => {
            if (!r.ok) return r.json().then(j => { throw new Error(j.error || ('HTTP ' + r.status)); });
            return r.blob();
        })
        .then(blob => {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'blocklist_' + bvid + '.json';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 5000);   // 稍延迟回收，避免下载未开始即失效
        })
        .catch(e => alert('屏蔽列表导出失败：' + e.message));
}

function reportRegen() {
    if (!confirm('重新生成 ' + BVID + ' 的报告？将清空该视频缓存并后台重跑完整分析流水线。')) return;
    const status = document.getElementById('reportJobStatus');
    status.textContent = '重新生成发起中…';
    fetch('/api/video/' + encodeURIComponent(BVID) + '/regenerate', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { status.textContent = j.error || '发起失败'; return; }
            status.textContent = '重新生成中（完整流水线，可能需要几分钟）…';
            reportPollRegen(j.job_id);
        })
        .catch(() => { status.textContent = '网络错误'; });
}

// 重新生成轮询连续失败计数：>=5 判定状态接口不可用，停止轮询并提示（参照 pollJob 熔断模式，独立计数防与手动分析轮询互相干扰）
let regenPollFails = 0;
function reportPollRegen(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            regenPollFails = 0;   // 成功响应清零连续失败计数
            const status = document.getElementById('reportJobStatus');
            if (j.error) { status.textContent = '任务状态查询失败: ' + j.error; return; }
            if (j.finished) {
                if (j.errors && j.errors.length) {
                    status.textContent = '重新生成失败: ' + j.errors[0].error;
                    return;
                }
                location.reload();
                return;
            }
            setTimeout(() => reportPollRegen(jobId), 3000);
        })
        .catch(() => {
            regenPollFails++;
            if (regenPollFails >= 5) {   // 熔断：job 接口持续异常，不再 3s 间隔无限轮询
                document.getElementById('reportJobStatus').textContent = '状态查询失败，请刷新页面查看';
                return;
            }
            setTimeout(() => reportPollRegen(jobId), 3000);
        });
}

function reportDelete() {
    if (!confirm('删除 ' + BVID + ' 的全部分析数据？\n包括：弹幕、发送者、用户画像、LLM 缓存（含共享深掘缓存）、全局映射、导出文件。\n注意：若其他视频涉及相同用户，其报告将缺数据。此操作不可恢复。')) return;
    fetch('/api/video/' + encodeURIComponent(BVID) + '/delete', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { alert(j.error || '删除失败'); return; }
            location.href = '/';
        })
        .catch(() => alert('网络错误'));
}
