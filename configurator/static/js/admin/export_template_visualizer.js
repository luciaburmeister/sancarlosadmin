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

        if (!previewIframe) return;

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

        const testObjectSelector = document.getElementById("test-object-selector");
        if (testObjectSelector) {
            testObjectSelector.addEventListener("change", () => {
                const objectId = testObjectSelector.value;
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
                // Also trigger a live update to use this object in POST
                updatePreview();
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
