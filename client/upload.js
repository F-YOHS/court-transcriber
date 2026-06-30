// Докачиваемая (chunked) загрузка файла.
// Делит файл на куски и шлёт их по очереди с возобновлением и повторами,
// чтобы медленный/рвущийся канал (релей Tailscale) не заставлял начинать
// загрузку заново. Совместимо со старым кодом: принимает ту же FormData,
// что и раньше уходила в POST /api/jobs, и возвращает {job_id, status}.
//
// Поток: POST /api/uploads/init -> PUT чанки -> POST /api/uploads/{id}/complete.
(function () {
    const CHUNK = 5 * 1024 * 1024;   // 5 МБ на кусок
    const MAX_RETRY = 6;             // подряд неудачных попыток на одном смещении

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const backoff = (n) => Math.min(15000, 1000 * 2 ** n);

    async function rfetch(url, opts) {
        const r = await fetch(url, opts);
        if (r.status === 401) {
            window.location.href = "/login";
            throw new Error("Сессия истекла");
        }
        return r;
    }

    async function jsonOrThrow(r) {
        if (!r.ok) {
            let detail = r.statusText;
            try { detail = (await r.json()).detail || detail; } catch (_) { /* noop */ }
            throw new Error(detail);
        }
        return r.json();
    }

    function storeKeyFor(file, mode) {
        return `ct_upload:${mode}:${file.name}:${file.size}:${file.lastModified}`;
    }

    function ensureBar() {
        let bar = document.getElementById("ct-upload-bar");
        if (bar) return bar;
        bar = document.createElement("div");
        bar.id = "ct-upload-bar";
        bar.style.cssText = [
            "position:fixed", "left:0", "right:0", "bottom:0", "z-index:9999",
            "background:#1f2430", "color:#fff", "font:14px/1.4 system-ui,sans-serif",
            "padding:12px 16px", "box-shadow:0 -2px 12px rgba(0,0,0,.3)",
        ].join(";");
        bar.innerHTML =
            '<div id="ct-upload-text" style="margin-bottom:8px"></div>' +
            '<div style="height:8px;background:rgba(255,255,255,.18);border-radius:4px;overflow:hidden">' +
            '<div id="ct-upload-fill" style="height:100%;width:0;background:#4f8cff;transition:width .2s"></div>' +
            '</div>';
        document.body.appendChild(bar);
        return bar;
    }

    function showBar(name, pct) {
        try {
            ensureBar();
            const t = document.getElementById("ct-upload-text");
            const f = document.getElementById("ct-upload-fill");
            if (t) t.textContent = `Передача файла на сервер: ${pct}% — «${name}». Не закрывайте страницу до завершения.`;
            if (f) f.style.width = `${pct}%`;
        } catch (_) { /* noop */ }
    }

    function hideBar() {
        try { document.getElementById("ct-upload-bar")?.remove(); } catch (_) { /* noop */ }
    }

    function setProgress(name, sent, total) {
        const pct = total ? Math.floor((sent / total) * 100) : 0;
        showBar(name, pct);
        try { document.title = `⬆ ${pct}% — ${name}`; } catch (_) { /* noop */ }
        try {
            window.dispatchEvent(new CustomEvent("upload:progress",
                { detail: { name, sent, total, pct } }));
        } catch (_) { /* noop */ }
    }

    async function initUpload(file) {
        const r = await rfetch("/api/uploads/init", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: file.name, size: file.size }),
        });
        return jsonOrThrow(r);                    // {upload_id, received}
    }

    async function getStatus(uploadId) {
        const r = await rfetch(`/api/uploads/${uploadId}`);
        if (r.status === 404) return null;
        return jsonOrThrow(r);                    // {received, size}
    }

    // PUT куска через XHR — в отличие от fetch, XHR даёт прогресс ОТДАЧИ
    // (upload.onprogress), поэтому полоса движется в реальном времени, а не
    // стоит на 0% до конца куска.
    function putChunk(uploadId, offset, buf, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("PUT", `/api/uploads/${uploadId}?offset=${offset}`);
            xhr.setRequestHeader("Content-Type", "application/octet-stream");
            if (onProgress) {
                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) onProgress(e.loaded);
                };
            }
            xhr.onload = () => {
                if (xhr.status === 401) {
                    window.location.href = "/login";
                    reject(new Error("Сессия истекла"));
                    return;
                }
                let data = {};
                try { data = JSON.parse(xhr.responseText); } catch (_) { /* noop */ }
                if (xhr.status === 409) {            // рассинхрон: сервер вернёт реальное смещение
                    resolve({ received: Number(data.received) || offset });
                    return;
                }
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(data);                   // {received}
                    return;
                }
                reject(new Error(data.detail || xhr.statusText || `HTTP ${xhr.status}`));
            };
            xhr.onerror = () => reject(new Error("ошибка сети"));
            xhr.ontimeout = () => reject(new Error("таймаут"));
            xhr.send(buf);
        });
    }

    async function completeUpload(uploadId, params) {
        const r = await rfetch(`/api/uploads/${uploadId}/complete`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                language: params.language || "ru",
                mode: params.mode || "classic",
                num_speakers: params.num_speakers ? Number(params.num_speakers) : null,
                initial_prompt: params.initial_prompt || null,
            }),
        });
        return jsonOrThrow(r);                    // {job_id, status}
    }

    async function resumableUpload(formData) {
        // Достаём файл и параметры из той же FormData, что строит handleFiles.
        const file = formData.get("file");
        if (!file) throw new Error("Файл не выбран");
        const params = {};
        for (const [k, v] of formData.entries()) {
            if (k !== "file") params[k] = v;
        }
        const mode = params.mode || "classic";
        const key = storeKeyFor(file, mode);
        const origTitle = document.title;

        try {
            // Пытаемся возобновить начатую ранее загрузку (после обрыва/перезагрузки).
            let uploadId = null;
            try { uploadId = localStorage.getItem(key); } catch (_) { /* noop */ }
            let received = 0;

            if (uploadId) {
                const st = await getStatus(uploadId).catch(() => null);
                if (st && Number(st.size) === file.size) {
                    received = Number(st.received) || 0;
                } else {
                    uploadId = null;              // устарело/не та сессия
                }
            }
            if (!uploadId) {
                const init = await initUpload(file);
                uploadId = init.upload_id;
                received = 0;
                try { localStorage.setItem(key, uploadId); } catch (_) { /* noop */ }
            }

            setProgress(file.name, received, file.size);

            let attempt = 0;
            while (received < file.size) {
                const end = Math.min(received + CHUNK, file.size);
                const before = received;
                const buf = await file.slice(before, end).arrayBuffer();
                try {
                    const res = await putChunk(uploadId, before, buf, (loaded) => {
                        setProgress(file.name, before + loaded, file.size);
                    });
                    received = Number(res.received);
                    if (received > before) {
                        attempt = 0;              // прогресс — сбрасываем счётчик
                    } else {
                        attempt += 1;             // 409 без сдвига / странность
                        if (attempt > MAX_RETRY) {
                            throw new Error("не удаётся продвинуть загрузку");
                        }
                        await sleep(backoff(attempt));
                    }
                } catch (err) {
                    attempt += 1;
                    if (attempt > MAX_RETRY) {
                        throw new Error(`обрыв загрузки (${err.message})`);
                    }
                    await sleep(backoff(attempt));
                    // Возможно, чанк дошёл, а ответ — нет. Спросим сервер.
                    const st = await getStatus(uploadId).catch(() => null);
                    if (st) received = Number(st.received) || received;
                }
                setProgress(file.name, received, file.size);
            }

            const job = await completeUpload(uploadId, params);
            try { localStorage.removeItem(key); } catch (_) { /* noop */ }
            return job;
        } finally {
            hideBar();
            document.title = origTitle;           // вернуть заголовок вкладки
        }
    }

    window.resumableUpload = resumableUpload;
})();
