async function apiFetch(url, opts = {}) {
    const r = await fetch(url, opts);
    if (r.status === 401) {
        window.location.href = "/login";
        throw new Error("Сессия истекла");
    }
    return r;
}

const API = {
    list:   () => apiFetch("/api/profiles").then(r => r.json()),
    create: (fd) => apiFetch("/api/profiles", { method: "POST", body: fd })
        .then(async r => {
            if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
            return r.json();
        }),
    remove: (id) => apiFetch(`/api/profiles/${id}`, { method: "DELETE" }).then(r => r.json()),
};

const toastEl = document.getElementById("toast");
let toastTimer = null;
function toast(msg, isError = false) {
    toastEl.textContent = msg;
    toastEl.classList.toggle("error", isError);
    toastEl.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.add("hidden"), 3500);
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit",
    });
}

// ----- Upload zone -----

const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const submitBtn = document.getElementById("submit-btn");
const dropTitle = document.getElementById("drop-title");
const nameInput = document.getElementById("profile-name");
let selectedFile = null;

function updateSubmitState() {
    submitBtn.disabled = !(selectedFile && nameInput.value.trim());
}

nameInput.addEventListener("input", updateSubmitState);

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});

["dragenter", "dragover"].forEach(ev =>
    dropZone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropZone.classList.add("drag-over");
    })
);
["dragleave", "drop"].forEach(ev =>
    dropZone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropZone.classList.remove("drag-over");
    })
);

dropZone.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) handlePickedFile(file);
});

fileInput.addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file) handlePickedFile(file);
});

function handlePickedFile(file) {
    selectedFile = file;
    dropTitle.textContent = file.name;
    updateSubmitState();
}

// ----- Submit -----

document.getElementById("add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!selectedFile || !nameInput.value.trim()) return;

    const fd = new FormData();
    fd.append("name", nameInput.value.trim());
    fd.append("file", selectedFile);

    submitBtn.disabled = true;
    submitBtn.textContent = "Считаю эмбеддинг…";
    try {
        await API.create(fd);
        toast(`Профиль «${nameInput.value.trim()}» сохранён`);
        // reset
        nameInput.value = "";
        selectedFile = null;
        fileInput.value = "";
        dropTitle.textContent = "Перетащите образец голоса";
        await refreshList();
    } catch (err) {
        toast(`Ошибка: ${err.message}`, true);
    } finally {
        submitBtn.textContent = "Сохранить голос";
        updateSubmitState();
    }
});

// ----- List -----

const listEl = document.getElementById("profiles-list");
document.getElementById("refresh-btn").addEventListener("click", refreshList);

async function refreshList() {
    try {
        const profiles = await API.list();
        renderList(profiles);
    } catch (err) {
        toast(`Не удалось загрузить: ${err.message}`, true);
    }
}

function renderList(profiles) {
    if (!profiles || profiles.length === 0) {
        listEl.innerHTML = `<div class="empty">Пока ни одного профиля. Добавьте первый выше.</div>`;
        return;
    }
    listEl.innerHTML = profiles.map(p => `
        <div class="job" data-id="${p.id}">
            <div class="job-main">
                <div class="job-name">${escapeHtml(p.name)}</div>
                <div class="job-meta">
                    <span>Создан ${formatDate(p.created_at)}</span>
                </div>
            </div>
            <div class="job-actions">
                <button class="danger-btn" data-action="delete" title="Удалить">✕</button>
            </div>
        </div>
    `).join("");

    listEl.querySelectorAll("[data-action='delete']").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const row = btn.closest(".job");
            const id = row.dataset.id;
            const name = row.querySelector(".job-name").textContent;
            if (!confirm(`Удалить профиль «${name}»?`)) return;
            await API.remove(id);
            toast("Удалено");
            await refreshList();
        });
    });
}

refreshList();
