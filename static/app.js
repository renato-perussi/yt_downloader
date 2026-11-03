const $ = (id) => document.getElementById(id);

// Tamanho aproximado em pt-BR ("~6,9 MB"). O backend usa filesize_approx,
// então o "~" deixa claro que é estimativa.
function fmtSize(n) {
  if (!n) return "—";
  const mb = n / 1048576;
  const s = mb >= 1000 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
  return `~${s.replace(".", ",")}`;
}

// "20180124" → "24/01/2018". Formato cru do yt-dlp, ilegível na UI.
function formatUploadDate(yyyymmdd) {
  const m = String(yyyymmdd || "").match(/^(\d{4})(\d{2})(\d{2})$/);
  if (!m) return "";
  return `${m[3]}/${m[2]}/${m[1]}`;
}

// Views em pt-BR compacto: "6,1 mi de visualizações".
function formatViews(views) {
  const v = Number(views);
  if (!Number.isFinite(v)) return "";
  if (v >= 1000000) return `${(v / 1000000).toFixed(1).replace(".", ",")} mi de visualizações`;
  if (v >= 1000) return `${(v / 1000).toFixed(1).replace(".", ",")} mil visualizações`;
  if (v === 1) return "1 visualização";
  return `${v.toLocaleString("pt-BR")} visualizações`;
}

// Normaliza o "detail" do FastAPI: string (4xx do dominio), lista de
// objetos (422 de validacao) ou ausente (body nao-JSON) → sempre texto.
function errorMessage(data) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((item) => item && item.msg).filter(Boolean);
    if (msgs.length) return msgs.join(" · ");
  }
  return "Falha ao buscar";
}

// Hero centralizado → topo: o bloco inicial ocupa a altura da viewport
// (texto + busca centralizados verticalmente); após a primeira busca ele
// recolhe para o topo e o card aparece abaixo.
function dockHero() {
  const hero = $("hero");
  if (!hero) return;
  if (hero.dataset.docked === "1") return;
  hero.dataset.docked = "1";
  hero.classList.remove("justify-center", "min-h-[calc(100vh-4rem)]");
  hero.classList.add("justify-start");
}

async function fetchInfo() {
  // Guard de concorrência: evita dois fetches simultâneos (Enter repetido)
  // enquanto o primeiro request ainda está em andamento.
  if (document.getElementById("fetch-btn").disabled) return;
  const url = $("url-input").value.trim();
  $("status").textContent = "";
  $("result").classList.add("hidden");
  if (!url) { $("status").textContent = "Cole um link do YouTube."; return; }
  dockHero();
  $("fetch-btn").disabled = true;
  $("status").textContent = "Buscando infos...";
  try {
    const r = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errorMessage(data));
    render(data, url);
    $("status").textContent = "";
  } catch (e) {
    $("status").textContent = e.message;
  } finally {
    $("fetch-btn").disabled = false;
  }
}

// Estado da seleção atual: formatos do último /api/info + tipo escolhido.
let cachedFormats = [];
let cachedUrl = "";
let currentType = "video";

// Altura em px a partir de "1920x1080", "1280×720" ou "720p". Sem altura → -1.
function parseHeight(res) {
  if (!res) return -1;
  const wh = String(res).match(/(\d+)\s*[x×]\s*(\d+)/);
  if (wh) return parseInt(wh[2], 10);
  const p = String(res).match(/(\d+)\s*p/i);
  if (p) return parseInt(p[1], 10);
  return -1;
}

// Melhor qualidade primeiro: vídeo por altura desc, áudio por bitrate desc.
// Sem tamanho/bitrate conhecido vai para o fim, nunca para o topo.
function sortByQuality(a, b) {
  if (currentType === "video") {
    const dh = parseHeight(b.resolution) - parseHeight(a.resolution);
    if (dh !== 0) return dh;
  } else {
    const da = (b.abr || -1) - (a.abr || -1);
    if (da !== 0) return da;
  }
  return (b.filesize || -1) - (a.filesize || -1);
}

function videoQualityName(f) {
  const h = parseHeight(f.resolution);
  if (h === 2160) return "2160p 4K";
  if (h === 1440) return "1440p QHD";
  if (h === 1080) return "1080p Full HD";
  if (h === 720) return "720p HD";
  if (h > 0) return `${h}p`;
  return f.resolution || "?";
}

function audioTierLabel(abr) {
  const v = Number(abr) || 0;
  if (v >= 120) return "Alta";
  if (v >= 60) return "Média";
  return "Baixa";
}

function qualityLabel(f) {
  if (currentType === "video") {
    return `${videoQualityName(f)} • ${f.ext} • ${fmtSize(f.filesize)}`;
  }
  // O backend transcodifica todo áudio para MP3 via ffmpeg
  // (yt-dlp ignora --extract-audio com saída em stdout), então o
  // rótulo mostra a faixa agrupada — o formato que chega no download.
  // Agrupamento aprovado: Alta/Média/Baixa (1 por faixa, sem-info oculto).
  return `${audioTierLabel(f.abr)} • MP3 • ${fmtSize(f.filesize)}`;
}

// Preenche o select único de qualidade com os formatos do tipo atual,
// ordenados da melhor para a pior qualidade.
function fillQuality() {
  const select = $("quality-select");
  select.innerHTML = "";
  const items = cachedFormats
    .filter((f) => (currentType === "video" ? f.kind === "video" && f.ext === "mp4" : f.kind === "audio"))
    .sort(sortByQuality);
  if (!items.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = currentType === "video" ? "Sem vídeo MP4" : "Sem áudio";
    select.appendChild(opt);
    select.disabled = true;
  } else {
    select.disabled = false;
    items.forEach((item, i) => {
      const opt = document.createElement("option");
      opt.value = item.format_id;
      // Rótulo via textContent (não innerHTML): os metadados vêm do
      // yt-dlp (resolution/format_note/ext) e não podem injetar HTML no DOM.
      // Primeira opção = melhor qualidade → marcada como recomendada.
      opt.textContent = qualityLabel(item) + (i === 0 ? " ★ Recomendada" : "");
      select.appendChild(opt);
    });
  }
  $("download-btn").textContent = "Download";
}

// Badge de estatística no padrão do Design System (section 3 — Badges):
// pill arredondado com ícone Lucide rose e texto neutro. O canal usa a
// variante de destaque (borda/fundo rose). Texto via textContent (dado yt-dlp).
function statItem(icon, text, accent = false) {
  const span = document.createElement("span");
  span.className = accent
    ? "inline-flex items-center gap-1.5 px-3 py-1 rounded-full border border-[#ff005e]/20 bg-[#ff005e]/5 text-xs font-medium text-[#ff005e] min-w-0 max-w-full"
    : "inline-flex items-center gap-1.5 px-3 py-1 rounded-full border border-neutral-200 dark:border-white/10 bg-white dark:bg-neutral-900 text-xs font-medium text-neutral-700 dark:text-neutral-300 min-w-0 max-w-full";
  span.innerHTML = `<span class="iconify ${accent ? "" : "text-[#ff005e] "}shrink-0" data-icon="${icon}" data-width="14"></span>`;
  const t = document.createElement("span");
  t.className = "min-w-0 break-words";
  t.textContent = text;
  span.appendChild(t);
  return span;
}

// Bloco de informações do vídeo em Badges (DS section 3):
// canal em destaque + duração/views/data como pills neutras.
function renderMeta(info) {
  const meta = $("meta");
  meta.innerHTML = "";
  meta.className = "mt-3 flex flex-wrap gap-2";
  if (info.channel && info.channel !== "—") meta.appendChild(statItem("lucide:user", info.channel, true));
  if (info.duration_str) meta.appendChild(statItem("lucide:clock", info.duration_str));
  const views = formatViews(info.views);
  if (views) meta.appendChild(statItem("lucide:eye", views));
  const date = formatUploadDate(info.upload_date);
  if (date) meta.appendChild(statItem("lucide:calendar", date));
}

function render(info, url) {
  dockHero();
  const thumb = $("thumb");
  // I2: só https (defesa em profundidade; resto já usa textContent).
  if (/^https:/.test(info.thumbnail || "")) thumb.src = info.thumbnail;
  else thumb.removeAttribute("src");
  $("title").textContent = info.title || "";
  renderMeta(info);
  cachedFormats = info.formats || [];
  cachedUrl = url;
  currentType = $("type-select").value === "audio" ? "audio" : "video";
  fillQuality();

  $("download-btn").onclick = () => {
    const format_id = $("quality-select").value;
    if (!format_id) return;
    const audioOnly = currentType === "audio";
    window.location.href = `/api/download?url=${encodeURIComponent(cachedUrl)}&format_id=${encodeURIComponent(format_id)}&audio_only=${audioOnly}`;
  };
  $("result").classList.remove("hidden");
}

$("type-select").addEventListener("change", () => {
  currentType = $("type-select").value === "audio" ? "audio" : "video";
  fillQuality();
});

$("fetch-btn").onclick = fetchInfo;
$("url-input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (document.getElementById("fetch-btn").disabled) return;
  fetchInfo();
});
