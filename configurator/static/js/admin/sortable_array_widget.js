function sortableArrayWidget(initialValues, fieldName) {
    return {
        fieldName: fieldName,
        items: initialValues.map((value, index) => ({
            key: `item-${index}-${Date.now()}-${Math.random()}`,
            value: value,
        })),
        dragIndex: null,
        add() {
            this.items.push({ key: `item-new-${Date.now()}-${Math.random()}`, value: "" });
        },
        remove(index) {
            this.items.splice(index, 1);
        },
        onDragStart(index) {
            this.dragIndex = index;
        },
        onDragOver(index) {
            if (this.dragIndex === null || this.dragIndex === index) return;
            const moved = this.items.splice(this.dragIndex, 1)[0];
            this.items.splice(index, 0, moved);
            this.dragIndex = index;
        },
        onDrop() {
            this.dragIndex = null;
        },
        onDragEnd() {
            this.dragIndex = null;
        },
    };
}
