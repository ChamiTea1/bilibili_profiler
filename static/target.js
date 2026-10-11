/* 目标报告只读取独立快照，不修改常规报告的判定或画像。 */
(() => {
    'use strict';
    const data = window.__TARGET__;
    const el = id => document.getElementById(id);
    let page = 1, pages = 1, hash = '', requestNo = 0;
    function tab(name) {
        if (!['overview', 'users', 'matches', 'comments'].includes(name)) name = 'overview';
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
        if (name === 'matches') load();
    }
    function restore() {
        const fragment = location.hash.slice(1);
        if (fragment.startsWith('uid-')) {
            tab('users');
            el(fragment)?.scrollIntoView({block: 'center'});
        } else tab(new URLSearchParams(fragment).get('tab') || 'overview');
    }
    function navigate(name) {
        const fragment = `#tab=${name}`;
        if (location.hash === fragment) tab(name);
        else location.hash = fragment;
    }
    async function load() {
        const current = ++requestNo;
        el('targetError').textContent = '';
        el('targetPage').textContent = '加载中…';
        el('targetPrev').disabled = el('targetNext').disabled = true;
        try {
            const params = new URLSearchParams({page, mid_hash: hash});
            const response = await fetch(`/api/target/${data.id}/danmaku?${params}`);
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || '请求失败');
            if (current !== requestNo) return;
            page = body.page;
            pages = body.pages;
            el('targetMatches').replaceChildren();
            body.rows.forEach(row => {
                const tr = document.createElement('tr');
                const time = document.createElement('td');
                const link = document.createElement('a');
                link.href = row.video_url;
                link.target = '_blank'; link.rel = 'noopener';
                link.textContent = row.time_label;
                time.append(link); tr.append(time);
                [row.content, row.sender_label, `${row.confidence} · ${row.method || '未解析'}`].forEach(value => {
                    const td = document.createElement('td'); td.textContent = value; tr.append(td);
                });
                el('targetMatches').append(tr);
            });
            el('targetPage').textContent = `第 ${page} / ${pages} 页 · 共 ${body.total} 条`;
            el('targetPrev').disabled = page <= 1;
            el('targetNext').disabled = page >= pages;
        } catch (error) {
            if (current !== requestNo) return;
            el('targetMatches').replaceChildren();
            el('targetError').textContent = `加载失败：${error.message}。点击“显示全部”可重试。`;
            el('targetPage').textContent = '';
        }
    }
    document.querySelectorAll('.tab-btn').forEach(b => b.addEventListener('click', () => {
        navigate(b.dataset.tab);
    }));
    document.querySelectorAll('.target-filter').forEach(b => b.addEventListener('click', () => {
        hash = b.dataset.hash; page = 1;
        el('targetScope').textContent = `发送者 hash：${hash}`;
        navigate('matches');
    }));
    el('targetReset').addEventListener('click', () => {
        hash = ''; page = 1; el('targetScope').textContent = '全部命中'; load();
    });
    el('targetPrev').addEventListener('click', () => {if (page > 1) {page--; load();}});
    el('targetNext').addEventListener('click', () => {if (page < pages) {page++; load();}});
    el('targetMask').addEventListener('click', () => {
        document.cookie = `mask=${data.mask ? '0' : '1'};path=/;SameSite=Lax`; location.reload();
    });
    document.querySelectorAll('.candidate-space').forEach(button => button.addEventListener('click', async () => {
        const result = button.parentElement.querySelector('.space-result');
        button.disabled = true;
        result.textContent = '正在按限速读取公开空间首页…';
        try {
            const response = await fetch(`/api/target/${data.id}/candidate-space`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mid_hash: button.dataset.hash, uid: button.dataset.uid})
            });
            const contentType = response.headers.get('content-type') || '';
            if (!contentType.includes('application/json')) throw new Error('服务暂不可用，请稍后重试');
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || '核验失败');
            // 仅接收本机路由的 HTML；公开原文与链接均由服务端转义、校验。
            result.innerHTML = body.html;
            button.textContent = '读取核验缓存';
        } catch (error) {
            result.textContent = `核验未完成：${error.message}`;
        } finally {
            button.disabled = false;
        }
    }));
    window.addEventListener('hashchange', restore);
    restore();
})();
