/* 可见名片串行读取；空间行为样本仍由用户点击核验，所有请求走本机限速客户端。 */
(() => {
    'use strict';
    const queue = [], attempted = new Set();
    let running = false, paused = false;
    const key = row => `${row.dataset.endpoint}|${row.dataset.hash}|${row.dataset.uid}`;
    const currentRow = row => [...document.querySelectorAll(row.matches('.crc-sender') ? '.crc-sender' : 'article.space-candidate')].find(
        item => key(item) === key(row) && !!item.closest('.crc-sender') === !!row.closest('.crc-sender'));
    const observer = new IntersectionObserver(entries => {
        for (const entry of entries) {
            const row = entry.target;
            if (entry.isIntersecting && !paused && row.dataset.cardReady === '0' && !attempted.has(key(row))) {
                attempted.add(key(row));
                observer.unobserve(row);
                queue.push({row, mode:row.matches('.crc-sender') ? 'reconcile' : 'card', automatic:true});
            }
        }
        drain();
    });
    function observeCards() {
        document.querySelectorAll('article.space-candidate[data-card-ready="0"], .crc-sender[data-reconcile="1"]').forEach(row => {
            if (!attempted.has(key(row))) observer.observe(row);
        });
    }
    function replaceGroup(row, body) {
        const group = row.closest('.crc-sender') || [...document.querySelectorAll('.crc-sender')].find(
            node => node.dataset.hash === row.dataset.hash && node.dataset.endpoint === row.dataset.endpoint);
        if (group && body.group_removed) {
            group.querySelectorAll('article.space-candidate').forEach(node => observer.unobserve(node));
            observer.unobserve(group);
            group.remove();
        } else if (group && body.group_html) {
            const detailKey = node => `${node.closest('.space-candidate')?.dataset.uid || 'evidence'}|${node.querySelector('summary')?.textContent}`;
            const opened = new Map([...group.querySelectorAll('details')].map(node => [detailKey(node), node.open]));
            const hash = group.dataset.hash, endpoint = group.dataset.endpoint;
            group.outerHTML = body.group_html; // 服务端已转义名片及弹幕原文。
            const updated = [...document.querySelectorAll('.crc-sender')].find(node => node.dataset.hash === hash && node.dataset.endpoint === endpoint);
            updated?.querySelectorAll('details').forEach(node => {
                if (opened.has(detailKey(node))) node.open = opened.get(detailKey(node));
            });
        }
        if (Number.isInteger(body.candidate_count) && body.candidate_count >= 0) {
            const pane = document.getElementById('tab-crc');
            if (pane) {
                if (body.candidate_count === 0 && body.empty_html) pane.innerHTML = body.empty_html;
                const container = pane.querySelector('.crc-candidates');
                if (container) container.dataset.count = String(body.candidate_count);
                pane.querySelectorAll('[data-crc-total]').forEach(node => {node.textContent = String(body.candidate_count);});
            }
            const tab = document.querySelector('[data-tab="crc"]');
            if (tab) tab.textContent = `纯破解候选（${body.candidate_count}）`;
        }
        observeCards();
    }
    async function drain() {
        if (running) return;
        running = true;
        try {
            while (queue.length) {
                const task = queue.shift();
                let row = currentRow(task.row) || task.row;
                if (!row.isConnected) continue;
                if (task.automatic && (paused || !row.getClientRects().length)) {
                    attempted.delete(key(row));
                    observer.observe(row);
                    continue;
                }
                const result = row.querySelector(task.mode === 'space' ? '.space-result' : '.candidate-card-note');
                const buttons = row.querySelectorAll('button');
                buttons.forEach(button => {button.disabled = true;});
                result.textContent = task.mode === 'space' ? '正在按限速读取公开空间首页…' : '正在核验名片…';
                try {
                    const response = await fetch(row.dataset.endpoint, {
                        method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({mid_hash:row.dataset.hash, uid:row.dataset.uid, mode:task.mode})
                    });
                    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('服务暂不可用');
                    const body = await response.json();
                    if (!response.ok) throw new Error(body.error || '核验失败');
                    replaceGroup(row, body);
                    row = currentRow(row);
                    if (task.mode === 'space' && row) {
                        row.querySelector('.space-result').innerHTML = body.html;
                        row.querySelector('.candidate-space').textContent = '读取核验缓存';
                    }
                    if (body.risk || (task.mode === 'card' && body.status === 'unknown')) {
                        paused = true;
                        if (row) {
                            row.querySelector('.candidate-card-note').textContent = body.risk ? '风控中止，候选保留；自动核验已暂停' : '名片核验未完成，候选保留；可重试继续';
                            row.querySelector('.candidate-card-retry').hidden = false;
                        }
                    }
                } catch (error) {
                    paused = true;
                    result.textContent = `核验未完成：${error.message}`;
                    const retry = row.querySelector('.candidate-card-retry');
                    if (retry) retry.hidden = false;
                } finally {
                    buttons.forEach(button => {button.disabled = false;});
                }
            }
        } finally {running = false;}
    }
    document.addEventListener('click', event => {
        const button = event.target.closest('.candidate-space, .candidate-card-retry');
        if (!button || button.disabled) return;
        const row = button.closest('article.space-candidate');
        if (!row) return;
        const mode = button.classList.contains('candidate-space') ? 'space' : 'card';
        paused = false;
        attempted.add(key(row));
        queue.push({row, mode, automatic:false});
        observeCards();
        drain();
    });
    // 图片不可用只换占位图，不能据此判断账号不存在。
    document.addEventListener('error', event => {
        if (event.target.matches?.('img.candidate-avatar')) {
            const placeholder = document.createElement('span');
            placeholder.className = 'candidate-avatar candidate-placeholder';
            placeholder.textContent = '?';
            event.target.replaceWith(placeholder);
        }
    }, true);
    observeCards();
})();
