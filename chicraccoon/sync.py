import json
import os
import os.path
import re
import shutil
import sqlite3
import struct
import sys

from jinja2 import Environment, PackageLoader, select_autoescape
from PIL import Image
from pkg_resources import ResourceManager, get_provider

from chicraccoon.enotebackup import EnoteBackup

def grayscale_to_mask(image):
    pixels = image.tobytes()
    new_pixels = []
    for pixel in pixels:
        new_pixels.append(0)
        new_pixels.append(0)
        new_pixels.append(0)
        new_pixels.append(255 - pixel)
    return Image.frombuffer('RGBA', image.size, bytes(new_pixels),
        'raw', 'RGBA', 0, 1)

class LocalNotebook:
    def __init__(self, path):
        self.path = path
        self.d = {
            'forms': {},
            'pages': {},
            'notebooks': {},
            'images': {}
        }

        if not os.path.exists(path):
            os.mkdir(path)

        if os.path.exists(self._path('data.json')):
            with open(self._path('data.json')) as f:
                # JSON doesn't allow integral keys, so they're actually
                # stored as strings. the hook converts them back.
                int_maybe = lambda x: int(x) if x.isnumeric() else x
                pairs_hook = lambda pairs: {int_maybe(k):v for k,v in pairs}
                self.d = json.load(f, object_pairs_hook=pairs_hook)

    def save(self):
        with open(self._path('data.json'), 'w') as f:
            json.dump(self.d, f)

    def _path(self, *parts):
        return os.path.join(self.path, *parts)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.save()

    def load_page_order(self, id_, backup):
        filename = 'PAGE/N{id:06X}/PAGE_ORDER.bin'.format(id=id_)
        data = backup.extract_file(backup.find_file(filename))
        return [x for (x, ) in struct.iter_unpack('<L', data)]

    def update_metadata(self, kind, db, backup):
        table_name = {
            'forms': 'forms',
            'pages': 'pages',
            'notebooks': 'notes'
            }[kind]

        self.d[kind] = {}

        # SQL prepared statements don't support placeholders in the
        # FROM clause
        cursor = db.execute('SELECT * FROM {}'.format(table_name))
        objects = cursor.fetchmany()

        while objects:
            for obj in objects:
                id_ = obj['id']

                if kind == 'forms':
                    self.d[kind][id_] = {
                        'notebook': obj['owner_id']
                    }
                elif kind == 'pages':
                    self.d[kind][id_] = {
                        'form': obj['form_id']
                    }
                elif kind == 'notebooks':
                    self.d[kind][id_] = {
                        'pages': self.load_page_order(id_, backup)
                    }

            objects = cursor.fetchmany()

        # uforms (imported forms) have owner_id = 0, just like built-in forms
        # which is kinda inconvenient, so we fix that
        if kind == 'forms':
            cursor = db.execute('SELECT * FROM uforms')
            uforms = cursor.fetchmany()
            while uforms:
                for uform in uforms:
                    self.d[kind][uform['form_id']]['notebook'] = -1
                uforms = cursor.fetchmany()

    def _image_path(self, basename, layer):
        basename = basename[:-4] # removing '.raw'
        return self._path('images', '{}_{}.png'.format(basename, layer))

    def _mkdir(self, *path):
        try:
            os.mkdir(self._path(*path))
        except FileExistsError:
            pass

    def convert_image(self, basename, image):
        for i, layer in enumerate(image.list_layers()):
            image = grayscale_to_mask(layer.to_pil())
            image.save(self._image_path(basename, i))

    def update_images(self, backup):
        self._mkdir('images')

        seen_images = set()
        for f in backup.list_files():
            filename = f.filename.decode('utf-8').lower()

            if f.is_dir:
                self._mkdir('images', filename)

            if not filename.endswith('.raw'):
                continue

            # flatten directories that are unnecessarily subdivided
            filename = re.sub(r'/[\da-f]{2}/', '/', filename)

            seen_images.add(filename)
            if filename not in self.d['images']:
                self.d['images'][filename] = {
                    'mtime': 0,
                    'layers': 0
                }

            if f.mtime > self.d['images'][filename]['mtime']:
                print('file {} updated, converting'.format(filename))
                image = backup.extract_image(f)
                self.d['images'][filename]['mtime'] = f.mtime
                self.d['images'][filename]['layers'] = image.layer_count()
                self.convert_image(filename, image)
            else:
                print('file {} not updated, skipping'.format(filename))

        files_to_delete = []
        for filename in self.d['images']:
            if filename not in seen_images:
                print('file {} deleted, deleting'.format(filename))
                for i in range(self.d['images'][filename]['layers']):
                    os.remove(self._image_path(filename, i))
                files_to_delete.append(filename)
        for filename in files_to_delete:
            del self.d['images'][filename]

    def regenerate_web(self):
        notebook_dirname = lambda x: 'n{:03}'.format(x)
        page_filename = lambda p, n: 'n{:03}/p{:06}.html'.format(n, p)
        notebook_covername = lambda x: 'images/note/n{:07x}_0.png'.format(x)
        def form_filename(id_, thumb=False):
            notebook = self.d['forms'][id_]['notebook']
            if notebook == -1:
                # uform
                return 'images/{thumb}uform/f{id:07x}_0.png'.format(
                    thumb='thumbnail/' if thumb else '', id=id_)
            elif notebook == 0:
                # built-in form
                return 'images/{thumb}form/f{id:07x}_0.png'.format(
                    thumb='thumbnail/' if thumb else '', id=id_)
            else:
                # imported form
                return 'images/{thumb}impt/n{nb:06x}/f{id:07x}_0.png'.format(
                    thumb='thumbnail/' if thumb else '', id=id_, nb=notebook)
        def page_imagename(id_, notebook, layer, thumb=False):
            return 'images/{thumb}page/n{nb:06x}/{tp}{id:07x}_{layer}.png'.format(
                thumb='thumbnail/' if thumb else '',
                tp='t' if thumb else 'p',
                id=id_, nb=notebook, layer=layer)


        # copy over static files
        self._mkdir('static')

        provider = get_provider('chicraccoon')
        static_dir = provider.get_resource_filename(
            ResourceManager(), 'web_static')
        for entry in os.scandir(static_dir):
            shutil.copy(entry.path, self._path('static'))

        # generate HTML
        env = Environment(
            loader=PackageLoader('chicraccoon', 'web_templates'),
            autoescape=select_autoescape(['html'])
        )

        # generate index page
        index_template = env.get_template('index.html')
        notebooks = []
        for id_ in self.d['notebooks']:
            cover = notebook_covername(id_)
            if not os.path.exists(self._path(cover)):
                cover = 'static/notebook_default.png'
            notebooks.append({
                'link': '{}/index.html'.format(notebook_dirname(id_)),
                'cover': cover
            })

        with open(self._path('index.html'), 'w') as f:
            f.write(index_template.render(notebooks=notebooks))

        # generate note and notebook pages
        notebook_template = env.get_template('notebook.html')
        page_template = env.get_template('notebook_page.html')
        for id_, notebook in self.d['notebooks'].items():
            self._mkdir(notebook_dirname(id_))

            pages = []
            page_ids = notebook['pages']
            for i, page_id in enumerate(page_ids):
                page = self.d['pages'][page_id]
                thumb_layers = [form_filename(page['form'], True)]
                layers = [form_filename(page['form'])]

                if os.path.exists(self._path(page_imagename(page_id, id_, 0))):
                    thumb_layers.append(page_imagename(page_id, id_, 0, True))
                    thumb_layers.append(page_imagename(page_id, id_, 1, True))
                    layers.append(page_imagename(page_id, id_, 0))
                    layers.append(page_imagename(page_id, id_, 1))

                prev_link = None
                if i != 0:
                    prev_link = page_filename(page_ids[i - 1], id_)

                next_link = None
                if i != len(page_ids) - 1:
                    next_link = page_filename(page_ids[i + 1], id_)

                with open(self._path(page_filename(page_id, id_)), 'w') as f:
                    f.write(page_template.render(
                        layers=layers,
                        base_dir='../',
                        page_num=i+1,
                        pages_total=len(page_ids),
                        prev_link=prev_link,
                        next_link=next_link))

                pages.append({'layers': thumb_layers,
                    'link': page_filename(page_id, id_)})

            with open(self._path(notebook_dirname(id_), 'index.html'), 'w') as f:
                f.write(notebook_template.render(pages=pages, base_dir='../'))


    def update(self, backup):
        with open(self._path('tmp.sqlite3'), 'wb') as f:
            f.write(backup.extract_file(backup.find_file('enotes.db3')))

        db = sqlite3.connect(self._path('tmp.sqlite3'))
        db.row_factory = sqlite3.Row

        self.update_metadata('forms', db, backup)
        self.update_metadata('notebooks', db, backup)
        self.update_metadata('pages', db, backup)

        db.close()
        os.remove(self._path('tmp.sqlite3'))

        self.update_images(backup)
        self.regenerate_web()

def main():
    if len(sys.argv) != 3:
        print('USAGE:')
        print('{} <notebook-directory> <path/to/enote.bkup>'.format(sys.argv[0]))
        return

    notebook_dir = sys.argv[1]
    backup_path = sys.argv[2]

    with LocalNotebook(notebook_dir) as notebook:
        with EnoteBackup(backup_path) as backup:
            notebook.update(backup)

if __name__ == '__main__':
    main()
