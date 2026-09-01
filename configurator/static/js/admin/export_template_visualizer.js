(function () {
    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== "") {
            const cookies = document.cookie.split(";");
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + "=")) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    function init() {
        const htmlInput = document.getElementById("id_html");
        const cssInput = document.getElementById("id_css");
        const previewIframe = document.getElementById("export-template-preview");
        const contextContent = document.getElementById("context-json-content");

        // Preview/context wiring only applies once the template has been saved at least
        // once (visualizar()/test_object_selector() both render a "save first" message,
        // not the iframe/dropdown, until then). AI-assist wiring below must NOT be inside
        // this guard - it's meant to work on a brand-new, unsaved template too.
        let refreshPreview = null;
        if (previewIframe) {
            refreshPreview = setupPreview();
        }

        setupAiAssist();

        function setupPreview() {
        const livePreviewUrl = previewIframe.dataset.livePreviewUrl;

        function updateContextJson(iframeDoc) {
            if (!contextContent) return;
            const contextScript = iframeDoc.getElementById("context-json");
            if (contextScript) {
                try {
                    const jsonData = JSON.parse(contextScript.textContent);
                    contextContent.textContent = JSON.stringify(jsonData, null, 4);
                } catch (e) {
                    console.error("Error parsing context JSON:", e);
                    contextContent.textContent = "Error parsing context JSON";
                }
            }
        }

        function updatePreview() {
            const formData = new FormData();
            formData.append("html", htmlInput.value);
            formData.append("css", cssInput.value);

            const kindSelect = document.getElementById("id_kind");
            const productSelect = document.getElementById("id_product");
            const companySelect = document.getElementById("id_company");

            if (kindSelect && kindSelect.value) {
                formData.append("kind", kindSelect.value);
            }
            if (productSelect && productSelect.value) {
                formData.append("product", productSelect.value);
            }
            if (companySelect && companySelect.value) {
                formData.append("company", companySelect.value);
            }

            const testObjectSelector = document.getElementById("test-object-selector");
            if (testObjectSelector && testObjectSelector.value) {
                formData.append("object_id", testObjectSelector.value);
            }

            fetch(livePreviewUrl, {
                method: "POST",
                body: formData,
                headers: {
                    "X-CSRFToken": getCookie("csrftoken")
                }
            })
                .then(response => response.text())
                .then(html => {
                    const doc = previewIframe.contentDocument || previewIframe.contentWindow.document;
                    doc.open();
                    doc.write(html);
                    doc.close();

                    // Update the context JSON display
                    updateContextJson(doc);
                })
                .catch(error => console.error("Error updating preview:", error));
        }

        let timeout = null;
        function debouncedUpdate() {
            clearTimeout(timeout);
            timeout = setTimeout(updatePreview, 2000);
        }

        if (htmlInput) {
            htmlInput.addEventListener("change", debouncedUpdate);
            htmlInput.addEventListener("input", debouncedUpdate);

            // Handle Unfold's WYSIWYG (usually Trix)
            const trixEditor = document.querySelector(`trix-editor[input="id_html"]`);
            if (trixEditor) {
                trixEditor.addEventListener("trix-change", debouncedUpdate);
            }
        }

        if (cssInput) {
            cssInput.addEventListener("input", debouncedUpdate);
        }

        // Initial preview and context load
        previewIframe.addEventListener("load", () => {
            const doc = previewIframe.contentDocument || previewIframe.contentWindow.document;
            updateContextJson(doc);
        });

        // A brand-new (unsaved) template has no per-pk GET url to rewrite - its src
        // starts blank and is only ever populated via the live POST endpoint below.
        const hasPkPreviewUrl = !!previewIframe.getAttribute("src");

        const testObjectSelector = document.getElementById("test-object-selector");
        if (testObjectSelector) {
            testObjectSelector.addEventListener("change", () => {
                const objectId = testObjectSelector.value;
                if (hasPkPreviewUrl) {
                    try {
                        const currentUrl = new URL(previewIframe.src, window.location.origin);
                        if (objectId) {
                            currentUrl.searchParams.set("object_id", objectId);
                        } else {
                            currentUrl.searchParams.delete("object_id");
                        }
                        previewIframe.src = currentUrl.toString();
                    } catch (e) {
                        console.error("Error updating preview URL:", e);
                    }
                }
                // Also trigger a live update to use this object in POST
                updatePreview();
            });
        }

        // Populate the preview immediately - needed for a new/unsaved template,
        // which has no per-pk src to load from otherwise.
        updatePreview();

        return updatePreview;
        }

        function setupAiAssist() {
            const promptInput = document.getElementById("ai-prompt-input");
            const generateButton = document.getElementById("ai-generate-button");
            const genericButton = document.getElementById("ai-generate-generic-button");
            const statusEl = document.getElementById("ai-generate-status");

            if (!generateButton && !genericButton) return;

            const GENERATION_TIMEOUT_MS = 90000;

            // Same icon-spinner convention Unfold itself uses (see its htmx-request
            // indicator in unfold/helpers/command.html): a Material Symbols glyph spun
            // via the compiled "animate-spin" utility, not a hand-rolled CSS circle.
            function makeSpinner() {
                const spinner = document.createElement("span");
                spinner.className = "material-symbols-outlined animate-spin text-[16px] leading-none";
                spinner.textContent = "progress_activity";
                return spinner;
            }

            // Same background/text color pairs Unfold uses for its own Django-messages
            // banners (unfold/helpers/messages/{success,error,primary}.html) - these are
            // real classes already present in the compiled stylesheet (Unfold ships a
            // prebuilt Tailwind bundle, not a runtime JIT, so only classes that already
            // appear somewhere in Unfold's own templates are safe to reuse here).
            // Position/layout stays inline since "fixed full-width top banner" isn't an
            // Unfold component - only the colors need to match its design system.
            const BANNER_VARIANT_CLASSES = {
                loading: "bg-primary-100 text-primary-700 dark:bg-primary-500/20 dark:text-primary-400",
                success: "bg-green-100 text-green-700 dark:bg-green-500/20 dark:text-green-400",
                error: "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-400",
            };

            // A fixed banner at the very top of the viewport - the small text next to
            // the button is easy to miss if you're scrolled past it, so generation
            // status also gets a banner nothing else on the page can hide.
            let banner = null;
            function getBanner() {
                if (banner) return banner;
                banner = document.createElement("div");
                banner.id = "ai-generate-banner";
                banner.style.cssText = "position: fixed; top: 0; left: 0; right: 0; z-index: 9999; display: none;";
                banner.className = "px-5 py-2.5 text-center text-sm font-medium flex items-center justify-center gap-2";
                document.body.prepend(banner);
                return banner;
            }

            function showBanner(message, variant) {
                const el = getBanner();
                el.className = "px-5 py-2.5 text-center text-sm font-medium flex items-center justify-center gap-2 "
                    + (BANNER_VARIANT_CLASSES[variant] || BANNER_VARIANT_CLASSES.loading);
                el.style.display = "flex";
                el.innerHTML = "";
                if (variant === "loading") el.appendChild(makeSpinner());
                el.appendChild(document.createTextNode(message));
            }

            function hideBanner(delayMs) {
                const el = getBanner();
                setTimeout(() => { el.style.display = "none"; }, delayMs || 0);
            }

            function setLoading(isLoading) {
                [generateButton, genericButton].forEach(btn => {
                    if (btn) btn.disabled = isLoading;
                });
                if (promptInput) promptInput.disabled = isLoading;
            }

            function setStatus(message, isLoading) {
                if (!statusEl) return;
                statusEl.innerHTML = "";
                if (isLoading) statusEl.appendChild(makeSpinner());
                statusEl.appendChild(document.createTextNode(message));
            }

            function runGeneration({ generic }) {
                const prompt = promptInput ? promptInput.value.trim() : "";
                if (!generic && !prompt) {
                    setStatus("Please enter a prompt, or use \"Generate Generic PDF\".", false);
                    return;
                }

                const formData = new FormData();
                if (generic) {
                    formData.append("generic", "1");
                } else {
                    formData.append("prompt", prompt);
                }
                formData.append("html", htmlInput ? htmlInput.value : "");
                formData.append("css", cssInput ? cssInput.value : "");

                const kindSelect = document.getElementById("id_kind");
                const productSelect = document.getElementById("id_product");
                const companySelect = document.getElementById("id_company");
                const testObjectSelector = document.getElementById("test-object-selector");

                if (kindSelect && kindSelect.value) formData.append("kind", kindSelect.value);
                if (productSelect && productSelect.value) formData.append("product", productSelect.value);
                if (companySelect && companySelect.value) formData.append("company", companySelect.value);
                if (testObjectSelector && testObjectSelector.value) formData.append("object_id", testObjectSelector.value);

                const url = (generateButton || genericButton).dataset.aiGenerateUrl;
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), GENERATION_TIMEOUT_MS);

                setLoading(true);
                setStatus("Generating... this can take up to 40 seconds.", true);
                showBanner("Generating your PDF with AI... this can take up to 40 seconds. Please wait.", "loading");

                fetch(url, {
                    method: "POST",
                    body: formData,
                    signal: controller.signal,
                    headers: {
                        "X-CSRFToken": getCookie("csrftoken")
                    }
                })
                    .then(response => response.json().then(data => ({ ok: response.ok, data })))
                    .then(({ ok, data }) => {
                        if (!ok) {
                            const message = data.error || "AI generation failed.";
                            setStatus(message, false);
                            showBanner(message, "error");
                            hideBanner(6000);
                            return;
                        }
                        if (htmlInput) htmlInput.value = data.html;
                        if (cssInput) cssInput.value = data.css;
                        setStatus("Done.", false);
                        showBanner("Done! Scroll down to see the preview.", "success");
                        hideBanner(4000);

                        // Refresh the live preview (no-op if the template hasn't been
                        // saved yet and the iframe doesn't exist).
                        if (refreshPreview) refreshPreview();
                    })
                    .catch(err => {
                        const message = err.name === "AbortError"
                            ? "AI generation timed out. Please try again."
                            : "AI generation failed.";
                        setStatus(message, false);
                        showBanner(message, "error");
                        hideBanner(6000);
                    })
                    .finally(() => {
                        clearTimeout(timeoutId);
                        setLoading(false);
                    });
            }

            if (generateButton) {
                generateButton.addEventListener("click", () => runGeneration({ generic: false }));
            }
            if (genericButton) {
                genericButton.addEventListener("click", () => runGeneration({ generic: true }));
            }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
