// 首页视频列表：搜索（标题/BV号）+ 列头排序（分析时间/弹幕数/画像人数）+ 分页（每页 20 条）
const IDX_PAGE_SIZE = 20;
const idxState = {kw: '', sortKey: 'time', asc: false, page: 1};
let idxRows = [];

function idxRender() {
    const tbody = document.getElementById('videoTbody');
    const hit = idxRows.filter(tr => !idxState.kw || tr.dataset.title.includes(idxState.kw));
    hit.sort((a, b) => {
        const k = idxState.sortKey;
        const va = a.dataset[k], vb = b.dataset[k];
        const cmp = (k === 'time') ? va.localeCompare(vb) : (parseFloat(va) - parseFloat(vb));
        return idxState.asc ? cmp : -cmp;
    });
    hit.forEach(tr => tbody.appendChild(tr));
    const pages = Math.max(1, Math.ceil(hit.length / IDX_PAGE_SIZE));
    idxState.page = Math.min(idxState.page, pages);
    const start = (idxState.page - 1) * IDX_PAGE_SIZE;
    const show = new Set(hit.slice(start, start + IDX_PAGE_SIZE));
    idxRows.forEach(tr => { tr.style.display = show.has(tr) ? '' : 'none'; });
    document.getElementById('idxPageInfo').textContent =
        hit.length > IDX_PAGE_SIZE ? '第 ' + idxState.page + ' / ' + pages + ' 页（共 ' + hit.length + ' 个视频）' : '';
    document.getElementById('idxPrev').disabled = idxState.page <= 1;
    document.getElementById('idxNext').disabled = idxState.page >= pages;
}

(function idxInit() {
    const tbody = document.getElementById('videoTbody');
    if (!tbody) return;
    idxRows = Array.from(tbody.querySelectorAll('tr[data-title]'));
    document.getElementById('idxSearch').addEventListener('input', function() {
        idxState.kw = this.value.trim().toLowerCase();
        idxState.page = 1;
        idxRender();
    });
    document.querySelectorAll('.video-table th[data-sort]').forEach(th =>
        th.addEventListener('click', () => {
            const k = th.dataset.sort;
            if (idxState.sortKey === k) idxState.asc = !idxState.asc;   // 同列再点翻转升降序
            else { idxState.sortKey = k; idxState.asc = false; }        // 换列默认降序
            idxState.page = 1;
            idxRender();
        }));
    document.getElementById('idxPrev').addEventListener('click', () => { idxState.page--; idxRender(); });
    document.getElementById('idxNext').addEventListener('click', () => { idxState.page++; idxRender(); });
    idxRender();
})();

// 跨视频屏蔽列表导出：读面板勾选（标准/最少命中视频数/上限）→ fetch 生成 → blob 下载。
// 无命中/服务端错误时 alert 提示而非下载空文件（与报告页单视频导出同模式）
function blkExportCross() {
    const crit = [
        document.getElementById('blkCringe').checked ? 'cringe' : '',
        document.getElementById('blkSpam').checked ? 'spam' : '',
        document.getElementById('blkCmt').checked ? 'cmt' : '',
    ].filter(Boolean);
    if (!crit.length) { alert('请至少勾选一项导出标准'); return; }
    const minVideos = parseInt(document.getElementById('blkMinVideos').value, 10) || 2;
    const max = parseInt(document.getElementById('blkMax').value, 10) || 200;
    const lowconf = document.getElementById('blkLowconf').checked ? '&lowconf=1' : '';
    const url = '/api/blocklist/cross?crit=' + encodeURIComponent(crit.join(',')) +
        '&min_videos=' + minVideos + '&max=' + max + lowconf;
    fetch(url)
        .then(r => {
            if (!r.ok) return r.json().then(j => { throw new Error(j.error || ('HTTP ' + r.status)); });
            return r.blob();
        })
        .then(blob => {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'blocklist_cross.json';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 5000);   // 稍延迟回收，避免下载未开始即失效
        })
        .catch(e => alert('跨视频屏蔽列表导出失败：' + e.message));
}
