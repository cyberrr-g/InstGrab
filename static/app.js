(function () {
  "use strict";

  const form = document.getElementById("download-form");
  const input = document.getElementById("url");
  const pasteBtn = document.getElementById("paste-btn");
  const submitBtn = document.getElementById("submit-btn");
  const btnLabel = submitBtn.querySelector(".btn-label");
  const alertBox = document.getElementById("alert");
  const alertText = document.getElementById("alert-text");
  const loading = document.getElementById("loading");
  const loadingStep = document.getElementById("loading-step");
  const results = document.getElementById("results");
  const resultThumb = document.getElementById("result-thumb");
  const resultThumbFallback = document.getElementById("result-thumb-fallback");
  const resultTitle = document.getElementById("result-title");
  const resultSub = document.getElementById("result-sub");
  const zipBtn = document.getElementById("zip-btn");
  const mediaGrid = document.getElementById("media-grid");
  const resetBtn = document.getElementById("reset-btn");
  const resetTopBtn = document.getElementById("reset-top-btn");
  const toast = document.getElementById("toast");

  const INSTAGRAM_URL =
    /^https?:\/\/(?:www\.|m\.)?instagram\.com\/(?:stories\/[A-Za-z0-9._]+(?:\/\d+)?|(?:p|reel|reels|tv)\/[A-Za-z0-9_-]+)\/?/i;

  const loadingSteps = [
    "Connecting to Instagram…",
    "Fetching media…",
    "Preparing your download…",
    "Still working — larger videos take a moment…"
  ];

  let stepTimer = null;
  let stepIndex = 0;
  let toastTimer = null;

  const fallbackIcon =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="3" y="3" width="18" height="18" rx="5" />' +
    '<circle cx="12" cy="12" r="4" />' +
    '<circle cx="17.5" cy="6.5" r="0.6" fill="currentColor" stroke="none" />' +
    "</svg>";

  const saveIcon =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 3v12" />' +
    '<path d="m7 10 5 5 5-5" />' +
    '<path d="M4 20h16" />' +
    "</svg>";

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.classList.remove("show");
    }, 2400);
  }

  function showError(message) {
    alertText.textContent = message;
    alertBox.hidden = false;
  }

  function hideError() {
    alertBox.hidden = true;
    alertText.textContent = "";
  }

  function setBusy(busy) {
    input.disabled = busy;
    pasteBtn.disabled = busy;
    submitBtn.disabled = busy;
    resetTopBtn.disabled = busy;
    submitBtn.classList.toggle("is-loading", busy);
    btnLabel.textContent = busy ? "Downloading…" : "Download";
  }

  function startLoadingSteps() {
    stepIndex = 0;
    loadingStep.textContent = loadingSteps[0];
    window.clearInterval(stepTimer);
    stepTimer = window.setInterval(function () {
      stepIndex = (stepIndex + 1) % loadingSteps.length;
      loadingStep.textContent = loadingSteps[stepIndex];
    }, 2400);
  }

  function stopLoadingSteps() {
    window.clearInterval(stepTimer);
    stepTimer = null;
  }

  function normalizeLink(value) {
    var trimmed = value.trim();
    if (trimmed && !/^https?:\/\//i.test(trimmed)) {
      trimmed = "https://" + trimmed;
    }
    return trimmed;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    var units = ["B", "KB", "MB", "GB"];
    var index = 0;
    var value = bytes;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    var digits = value >= 10 || index === 0 ? 0 : 1;
    return value.toFixed(digits) + " " + units[index];
  }

  function clearResults() {
    mediaGrid.replaceChildren();
    results.hidden = true;
    zipBtn.hidden = true;
    zipBtn.removeAttribute("href");
  }

  function setThumbnail(url) {
    resultThumb.onload = function () {
      resultThumb.hidden = false;
      resultThumbFallback.hidden = true;
    };
    resultThumb.onerror = function () {
      resultThumb.hidden = true;
      resultThumbFallback.hidden = false;
    };

    if (url) {
      resultThumb.src = url;
      resultThumb.alt = "Media preview";
    } else {
      resultThumb.removeAttribute("src");
      resultThumb.hidden = true;
      resultThumbFallback.hidden = false;
    }
  }

  function buildPreview(item) {
    var preview = document.createElement("div");
    preview.className = "media-preview";

    if (item.type === "video") {
      var video = document.createElement("video");
      video.controls = true;
      video.playsInline = true;
      video.preload = "none";
      if (item.thumbnail) video.poster = item.thumbnail;
      video.src = item.url;
      preview.appendChild(video);
      return preview;
    }

    if (item.type === "image") {
      var image = document.createElement("img");
      image.loading = "lazy";
      image.decoding = "async";
      image.src = item.url;
      image.alt = item.name;
      image.addEventListener("click", function () {
        window.open(item.url, "_blank", "noopener");
      });
      image.addEventListener("error", function () {
        var fallback = document.createElement("div");
        fallback.className = "preview-fallback";
        fallback.innerHTML = fallbackIcon + "<span>Preview unavailable</span>";
        preview.replaceChildren(fallback);
      });
      preview.appendChild(image);
      return preview;
    }

    var fileFallback = document.createElement("div");
    fileFallback.className = "preview-fallback";
    fileFallback.innerHTML = fallbackIcon + "<span>File ready</span>";
    preview.appendChild(fileFallback);
    return preview;
  }

  function buildMediaCard(item, index) {
    var card = document.createElement("article");
    card.className = "media-card";
    card.style.animationDelay = Math.min(index * 45, 300) + "ms";

    card.appendChild(buildPreview(item));

    var bar = document.createElement("div");
    bar.className = "media-bar";

    var info = document.createElement("div");
    info.className = "media-info";

    var name = document.createElement("span");
    name.className = "media-name";
    name.textContent = item.name;
    name.title = item.name;

    var size = document.createElement("span");
    size.className = "media-size";
    size.textContent = formatBytes(item.size) || "Ready";

    info.appendChild(name);
    info.appendChild(size);

    var link = document.createElement("a");
    link.className = "save-btn";
    link.href = item.download_url;
    link.setAttribute("download", item.name);
    link.innerHTML = saveIcon + "<span>Save</span>";

    bar.appendChild(info);
    bar.appendChild(link);
    card.appendChild(bar);

    return card;
  }

  function renderResults(data) {
    var items = Array.isArray(data.items) ? data.items : [];

    resultTitle.textContent = data.title || "Instagram media";
    resultTitle.title = data.title || "";

    var totalBytes = items.reduce(function (sum, item) {
      return sum + (Number(item.size) || 0);
    }, 0);

    var countText = items.length + (items.length === 1 ? " file" : " files");
    var sizeText = formatBytes(totalBytes);
    var whoText = data.uploader ? "@" + data.uploader : "Ready to save";

    resultSub.textContent = [countText, sizeText, whoText]
      .filter(Boolean)
      .join(" · ");

    setThumbnail(data.thumbnail);

    mediaGrid.replaceChildren();
    items.forEach(function (item, index) {
      mediaGrid.appendChild(buildMediaCard(item, index));
    });

    if (data.zip_url) {
      zipBtn.href = data.zip_url;
      zipBtn.hidden = false;
    } else {
      zipBtn.hidden = true;
    }

    results.hidden = false;
    window.requestAnimationFrame(function () {
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  async function requestDownload(url) {
    var controller = new AbortController();
    var timeout = window.setTimeout(function () {
      controller.abort();
    }, 260000);

    try {
      var response = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
        signal: controller.signal
      });

      var data = null;
      try {
        data = await response.json();
      } catch (error) {
        data = null;
      }

      if (!response.ok) {
        throw new Error(
          (data && data.error) ||
            "The server couldn't download that link. Please try again."
        );
      }

      if (!data || !Array.isArray(data.items) || data.items.length === 0) {
        throw new Error("No media was found at that link.");
      }

      return data;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("The request timed out. Please try a shorter video.");
      }
      if (error instanceof TypeError) {
        throw new Error(
          "Can't reach the server. Make sure the Django server is running and try again."
        );
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();

    var url = normalizeLink(input.value);

    if (!url) {
      showError("Paste an Instagram link first.");
      input.focus();
      return;
    }

    if (!INSTAGRAM_URL.test(url)) {
      showError(
        "That doesn't look like an Instagram story, post or reel link. Try a URL like instagram.com/reel/…"
      );
      input.focus();
      return;
    }

    input.value = url;
    hideError();
    clearResults();
    setBusy(true);
    startLoadingSteps();
    loading.hidden = false;

    try {
      var data = await requestDownload(url);
      loading.hidden = true;
      renderResults(data);
    } catch (error) {
      loading.hidden = true;
      showError(error.message || "Something went wrong. Please try again.");
    } finally {
      stopLoadingSteps();
      setBusy(false);
    }
  });

  pasteBtn.addEventListener("click", async function () {
    try {
      var text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        input.value = text.trim();
        hideError();
        showToast("Link pasted");
        input.focus();
      } else {
        showToast("Clipboard is empty");
        input.focus();
      }
    } catch (error) {
      input.focus();
      showToast("Press Ctrl/Cmd + V to paste");
    }
  });

  function resetForm() {
    clearResults();
    hideError();
    input.value = "";
    input.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  resetBtn.addEventListener("click", resetForm);
  resetTopBtn.addEventListener("click", resetForm);

  input.addEventListener("input", function () {
    if (!alertBox.hidden) hideError();
  });
})();
