"""Resolve screenshot references from the owning main node, never arbitrary file paths."""
import base64


def references(records, kind, output):
    for record in records[:3]:
        images = record.get('images', [])
        count = images if isinstance(images, int) else len(images)
        for index in range(min(count, 4)):
            if len(output) >= 8:
                return
            output.append({'kind': kind, 'id': record['id'], 'index': index})


def resolve(references):
    from . import canvas, teach_store
    parts, total = [], 0
    for ref in references:
        found = (canvas.store.marks_image(ref.id, ref.index) if ref.kind == 'marks'
                 else teach_store.image(ref.id, ref.index))
        if found is None:
            raise ValueError('A referenced screenshot is no longer available; point or record it again')
        data, media = found
        total += len(data)
        if len(data) > 8 * 1024 * 1024 or total > 24 * 1024 * 1024:
            raise ValueError('Screenshots exceed the image input size limit')
        parts.append({'type': 'image_url', 'image_url': {
            'url': 'data:' + media + ';base64,' + base64.b64encode(data).decode('ascii')}})
    return parts
