/* 候选报告只展示命令行保存的数据；打开页面不发起候选采集或核验。 */
(() => {
    'use strict';
    document.addEventListener('error', event => {
        if (event.target.matches?.('img.candidate-avatar')) {
            const placeholder = document.createElement('span');
            placeholder.className = 'candidate-avatar candidate-placeholder';
            placeholder.textContent = '?';
            event.target.replaceWith(placeholder);
        }
    }, true);
})();
