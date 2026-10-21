// ==UserScript==
// @name         WelearnBrainBurst 模式 2 · 浏览器自动答题
// @namespace    https://github.com/GDUTMeow/WelearnBrainBurst
// @version      1.0.0
// @description  自动完成 Welearn 课程页面的作答与提交（行为来自 KeqingMoe/WeAutomaticallyLearn，MIT License）
// @author       GamerNoTitle / KeqingMoe
// @match        https://welearn.sflep.com/*
// @match        http://welearn.sflep.com/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/*!
 * mode2-autolearn.js —— WelearnBrainBurst 模式 2：浏览器端自动答题脚本
 *
 * 用法：在浏览器中登录 Welearn 并打开课程学习页面（StudyCourse.aspx，页面内包含
 * 课程内容 iframe），把本脚本粘贴到浏览器控制台（F12 → Console）回车运行，
 * 或者安装为 Tampermonkey 用户脚本（文件顶部自带 UserScript 头，可直接导入）。
 * 运行后可用 WAL.stop() 停止、WAL.start() 重新开始。
 *
 * 运行时控制：
 *   WAL.stop()                 停止自动答题
 *   WAL.start()                重新开始自动答题
 *   WAL.tickOnce()             手动执行一次
 *   WAL.config.interval = 1500 调整轮询间隔（毫秒，不要小于 800）
 *
 * 本脚本的行为来自 https://github.com/KeqingMoe/WeAutomaticallyLearn （MIT License）：
 *
 * MIT License
 *
 * Copyright (c) 2025 KeqingMoe
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
(function () {
    'use strict';

    // Selenium 可能在页面刷新后重复注入；保持单实例，避免多个定时器重复提交。
    if (window.WAL && window.WAL.__wbb) {
        window.WAL.start();
        return;
    }

    const CONFIG = {
        // 轮询间隔（毫秒），尽量不要改小，最好不要小于 800
        interval: 1000,
        // 课程内容 iframe 的 id
        iframeId: 'contentFrame',
        // 未提交时自动提交
        autoSubmit: true,
        // 每题完成后自动跳转下一题
        autoNext: true,
        // 输出运行日志
        verbose: true,
    };

    const PREFIX = '[WelearnBrainBurst 模式2]';
    let timer = null;
    let running = false;
    const stats = {
        submits: 0,
        nextClicks: 0,
        lastActionAt: Date.now(),
        lastSubmitAt: 0,
        startedAt: Date.now(),
    };
    // 已处理过的页面签名，保证同一页只提交一次、只点一次下一题
    const submittedPages = new Set();
    const advancedPages = new Set();
    // Selenium 通过这个只读状态桥轮询答题进度；手动运行时也可查看 WAL.stats()。
    window.WAL_STATE = stats;

    const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

    function log(...args) {
        if (CONFIG.verbose) {
            console.log('%c' + PREFIX, 'color:#3b82f6;font-weight:bold', ...args);
        }
    }

    function warn(...args) {
        console.warn('%c' + PREFIX, 'color:#ef4444;font-weight:bold', ...args);
    }

    /** 判断某个 document 是否是题目所在文档 */
    function hasExercise(doc) {
        try {
            return !!doc && !!doc.querySelector('div[data-controltype="submitinfo"]');
        } catch (e) {
            return false;
        }
    }

    /** 取得课程内容 iframe 的 document，取不到时返回 null */
    function getIframeDocument() {
        // 真实站点并不总是把练习框命名为 contentFrame（实测某些课程没有这个 id），
        // 因此逐个 iframe 检查谁真正装着题目，再退回原来的 id 方案。
        const frames = document.querySelectorAll('iframe');
        for (const frame of frames) {
            try {
                const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                if (hasExercise(doc)) return doc;
            } catch (e) {
                /* 跨域 frame 直接跳过 */
            }
        }

        const iframe = document.getElementById(CONFIG.iframeId);
        // Selenium 也可能切换到练习 frame 后直接注入本脚本；此时当前 document
        // 就是课程内容页，不再有外层 iframe。
        if (!iframe) {
            return hasExercise(document) ? document : null;
        }
        try {
            return iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document) || null;
        } catch (e) {
            warn('无法访问课程 iframe（可能是跨域），请在课程学习页面本页运行本脚本', e);
            return null;
        }
    }

    /** 触发 input/change 事件，保证页面自身的事件监听能够记录填入的内容 */
    function fireInputEvents(el) {
        try {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        } catch (e) {
            /* 忽略：部分元素不支持事件派发 */
        }
    }

    /** 把参考答案填入题目控件，取自上游项目的作答逻辑 */
    function fillAnswers(doc) {
        // 先清掉不该选的选项，再选中标准答案对应的选项
        doc.querySelectorAll('li[data-choiced]:not([data-solution])').forEach((el) => el.click());
        doc.querySelectorAll('li[data-solution]:not([data-choiced])').forEach((el) => el.click());

        const results = doc.querySelectorAll('div[data-itemtype="result"]');
        results.forEach((result) => {
            try {
                const solution = result.textContent;
                const answer = solution
                    .split('/')[0]
                    .replace('(Answers may vary.)', '')
                    .replace(/[ \n]+/g, ' ')
                    .trim();

                const scope = result.closest('div').parentElement;

                const input = scope.querySelector('input[data-itemtype="input"]');
                if (input) {
                    input.value = answer;
                    fireInputEvents(input);
                }

                const myResult = scope.querySelector('div[data-itemtype="myresult"]');
                if (myResult) {
                    myResult.innerHTML = answer;
                }

                const choiceList = result.closest('div[data-controltype="choice"]');
                if (choiceList) {
                    choiceList.querySelectorAll('li[data-solution=""]').forEach((option) => {
                        option.setAttribute('data-choiced', '');
                    });
                }

                const textarea = scope.querySelector('textarea[data-itemtype="textarea"]');
                if (textarea) {
                    textarea.value = answer;
                    fireInputEvents(textarea);
                }
            } catch (e) {
                warn('处理题目答案失败，已跳过该题', e);
            }
        });
    }

    /** 取 submitinfo 文案，用于判断提交是否真的生效 */
    function submitInfoText(doc) {
        const info = doc.querySelector('div[data-controltype="submitinfo"]');
        return info ? info.textContent.replace(/\s/g, '') : '';
    }

    /** 判断当前题目是否尚未提交 */
    function isUnsubmitted(doc) {
        return submitInfoText(doc) === '尚未提交。';
    }

    /**
     * 当前页面签名：由题干文本决定。
     * 用于幂等保护——同一个页面绝不重复提交、重复点下一题。
     */
    function documentSignature(doc) {
        // 同一份试卷的不同题目可能没有 stem 文本，因此把页面地址也纳入签名，
        // 避免不同题目被判成同一页而漏答。
        const location = doc.location ? `${doc.location.pathname}${doc.location.search}` : '';
        const stems = Array.from(doc.querySelectorAll('[data-itemtype="stem"]'))
            .map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim())
            .join('|');
        const raw = `${location}#${stems}`;
        let hash = 0;
        for (let i = 0; i < raw.length; i += 1) {
            hash = (hash * 31 + raw.charCodeAt(i)) % 2147483647;
        }
        return `${hash}-${raw.length}`;
    }

    /** 提交当前题目（含“确认提交”弹层），只有确认生效后才计数 */
    async function submitCurrent(doc) {
        const submitBtn = doc.querySelector('a[data-controltype="submit"]');
        if (!submitBtn) return false;
        const before = submitInfoText(doc);
        submitBtn.click();
        await sleep(400);
        const confirmBtn = doc.querySelector('a.layui-layer-btn0');
        if (confirmBtn) confirmBtn.click();
        // 站点提交是异步的，等文案真正变化，避免同一个页面被反复提交。
        for (let i = 0; i < 6; i += 1) {
            await sleep(500);
            if (submitInfoText(doc) !== before) {
                stats.submits += 1;
                stats.lastSubmitAt = Date.now();
                stats.lastActionAt = stats.lastSubmitAt;
                return true;
            }
        }
        warn('提交后页面状态未变化，本轮不再重试该页面');
        return false;
    }

    /** 跳转下一题 */
    function goNext(doc) {
        const next = doc.querySelector('div.nextcmd');
        if (!next) return false;
        const disabled = next.hasAttribute('disabled')
            || next.getAttribute('aria-disabled') === 'true'
            || next.getAttribute('data-disabled') === 'true'
            || next.classList.contains('disabled');
        if (disabled) return false;
        next.click();
        stats.nextClicks += 1;
        stats.lastActionAt = Date.now();
        return true;
    }

    let lastMissingNotice = 0;
    function noticeIframeMissing() {
        const now = Date.now();
        if (now - lastMissingNotice > 10000) {
            lastMissingNotice = now;
            warn(`没有找到 #${CONFIG.iframeId}（课程内容 iframe），请确认当前页面是课程学习页面`);
        }
    }

    /** 一次轮询：作答 → 提交 → 下一题 */
    async function tick() {
        const doc = getIframeDocument();
        if (!doc) {
            noticeIframeMissing();
            return;
        }
        try {
            const signature = documentSignature(doc);
            stats.page = signature;
            const unsubmitted = isUnsubmitted(doc);
            // 页面本身已经是“已提交”状态时不再点下一题：那样会在单元里乱翻，
            // 把没打算处理的小节也翻出来。
            stats.pageSettled = !unsubmitted;
            if (unsubmitted && !submittedPages.has(signature)) {
                fillAnswers(doc);
                if (CONFIG.autoSubmit) {
                    await sleep(250);
                    if (await submitCurrent(doc)) {
                        submittedPages.add(signature);
                        stats.pageSettled = true;
                        if (CONFIG.autoNext && !advancedPages.has(signature)) {
                            if (goNext(doc)) {
                                advancedPages.add(signature);
                            }
                        }
                    }
                }
            }
        } catch (e) {
            warn('本轮处理出错，将在下一轮重试', e);
        }
    }

    function start() {
        if (running) return;
        running = true;
        if (timer) clearInterval(timer);
        timer = setInterval(tick, Math.max(800, Number(CONFIG.interval) || 1000));
        log(`已启动自动答题，每 ${Math.max(800, Number(CONFIG.interval) || 1000)} ms 检查一次（WAL.stop() 可停止）`);
        tick();
    }

    function stop() {
        running = false;
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
        log('已停止自动答题');
    }

    /** 同步轮询间隔（修改 CONFIG.interval 后调用 WAL.applyInterval() 生效） */
    function applyInterval() {
        if (running) {
            stop();
            start();
        }
    }

    const api = {
        __wbb: true,
        start,
        stop,
        tickOnce: tick,
        applyInterval,
        config: CONFIG,
        stats: () => ({ ...stats, running }),
        isRunning: () => running,
    };

    // 同时挂到两个名字上，方便用户记忆
    window.WAL = api;
    window.WeAutomaticallyLearn = api;

    if (!/welearn\.sflep\.com/.test(location.hostname)) {
        warn('当前页面看起来不是 Welearn 站点，脚本可能无法工作');
    }

    start();
})();
