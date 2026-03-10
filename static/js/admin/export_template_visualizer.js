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

        if (!previewIframe) return;

        const livePreviewUrl = previewIframe.dataset.livePreviewUrl;

        function updatePreview() {
            const formData = new FormData();
            formData.append("html", htmlInput.value);
            formData.append("css", cssInput.value);

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
                })
                .catch(error => console.error("Error updating preview:", error));
        }

        let timeout = null;
        function debouncedUpdate() {
            clearTimeout(timeout);
            timeout = setTimeout(updatePreview, 500);
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

        // Initial preview
        // updatePreview();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
