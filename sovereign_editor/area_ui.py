"""Native area composer: stage several explicit edits, review, save/undo together."""
import copy
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QPixmap,QIcon,QColor,QPen,QBrush
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,
    QTabWidget,QWidget,QComboBox,QSpinBox,QLineEdit,QPlainTextEdit,QListWidget,QListWidgetItem,
    QAbstractItemView,QScrollArea,QSplitter,QGraphicsView,QGraphicsScene,QGraphicsItem,QCheckBox)
from . import area_layout,world,npc_behavior
from .formats import EditorError


def spin(low,high,value):
    widget=QSpinBox();widget.setRange(low,high);widget.setValue(value);return widget


class AreaPreviewView(QGraphicsView):
    def __init__(self,editor):
        super().__init__();self.editor=editor

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            point=self.mapToScene(event.position().toPoint())
            x,z=int(point.x()//32),int(point.y()//32)
            if 0<=x<32 and 0<=z<32:
                prefix={0:'surface',2:'template',3:'dialogue'}.get(self.editor.tabs.currentIndex())
                if prefix:
                    getattr(self.editor,prefix+'_x').setValue(x+self.editor.ctx['origin'][0])
                    getattr(self.editor,prefix+'_z').setValue(z+self.editor.ctx['origin'][1])
        super().mousePressEvent(event)


class AreaEditor(QDialog):
    def __init__(self,inspector):
        super().__init__(inspector)
        self.inspector=inspector;self.project=inspector.project
        self.context=dict(header=inspector.header,cell=list(inspector.cell))
        self.ctx=self.project.context(**self.context);self.revision=self.project.doc['revision']
        self.operations=[];self.plan=None;self.preview_project=self.project
        self.setWindowTitle('Sovereign Editor · Area authoring');self.resize(1120,800)
        layout=QVBoxLayout(self)
        title=QLabel('Build an area');title.setStyleSheet('font-size:22px;font-weight:600');layout.addWidget(title)
        shared=area_layout.resource_users(self.project,self.ctx)
        sharing=QLabel(f"{self.ctx['name']} · {self.ctx['id']} · revision {self.revision}\n"
                       f"Shared map resource: headers {', '.join(str(u['header']) for u in shared['maps'])}. "
                       f"Events: {shared['events']}; scripts: {shared['scripts']}; text: {shared['texts']}.")
        sharing.setWordWrap(True);layout.addWidget(sharing)
        split=QSplitter();layout.addWidget(split,1)
        self.tabs=QTabWidget();self.tabs.setMinimumWidth(370);split.addWidget(self.tabs)
        self.tabs.addTab(self.surface_panel(),'Surfaces');self.tabs.addTab(self.group_panel(),'Group')
        self.tabs.addTab(self.library_panel(),'Library');self.tabs.addTab(self.dialogue_panel(),'Dialogue')
        right=QWidget();rl=QVBoxLayout(right);self.preview_tabs=QTabWidget();rl.addWidget(self.preview_tabs,1)
        self.before_view=self.image_view();self.after_view=self.image_view()
        self.preview_tabs.addTab(self.before_view,'Before');self.preview_tabs.addTab(self.after_view,'Preview')
        legend=QLabel('N · NPC    B · sign    W · entrance    Dashed · NPC range\nTeal · cleared collision    Red · blocked')
        legend.setWordWrap(True);rl.addWidget(legend)
        self.queue=QListWidget();self.queue.setMaximumHeight(130);rl.addWidget(self.queue)
        split.addWidget(right);split.setSizes([410,690]);split.setStretchFactor(1,1)
        self.status=QLabel('Choose changes and add them to the preview. Apply saves the whole area edit; Undo restores it together.')
        self.status.setWordWrap(True);layout.addWidget(self.status)
        buttons=QHBoxLayout();layout.addLayout(buttons)
        clear=QPushButton('Clear preview');clear.clicked.connect(self.clear);buttons.addWidget(clear)
        buttons.addStretch();cancel=QPushButton('Cancel');cancel.clicked.connect(self.reject);buttons.addWidget(cancel)
        self.apply_button=QPushButton('Apply area edit');self.apply_button.setObjectName('primary');self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply);buttons.addWidget(self.apply_button)
        self.show_image(self.before_view,self.project.map_scene(**self.context)['image'])
        self.show_image(self.after_view,self.project.map_scene(**self.context)['image'])

    def image_view(self):
        v=AreaPreviewView(self);v.setScene(QGraphicsScene(v));v.setMinimumSize(350,350);return v

    def show_image(self,view,path):
        view.scene().clear();view.scene().addPixmap(QPixmap(str(path)));view.setSceneRect(view.scene().itemsBoundingRect())
        project=self.preview_project if view is self.after_view else self.project
        ox,oz=self.ctx['origin'];member=self.ctx['map_member'];raw=self.project.member_raw(member)
        if view is self.after_view:
            old=self.project.composed()['permissions'];new=project.composed()['permissions']
            for z in range(32):
                for x in range(32):
                    offset=world.cell_offset(self.ctx,x+ox,z+oz);base=raw[offset:offset+2]
                    before=old.get((member,offset),base);after=new.get((member,offset),base)
                    if before==after:continue
                    color=QColor('#ef736d' if after[1]&128 else '#49d5c0')
                    pen=QPen(color);color.setAlpha(80)
                    tile=view.scene().addRect(x*32,z*32,32,32,pen,QBrush(color))
                    tile.setToolTip(f'{x+ox},{z+oz}: {before.hex()} → {after.hex()}')
        for e in project.map_events(**self.context)['events']:
            if not e['in_cell'] or e['kind']=='trigger':continue
            x,z=(e['x']-ox)*32,(e['z']-oz)*32
            color=QColor({'npc':'#ffd47d','warp':'#78c9ff','background':'#d39df7'}[e['kind']])
            if e['kind']=='npc' and (e.get('range_x',0) or e.get('range_z',0)):
                rx,rz=e['range_x'],e['range_z'];fill=QColor(color);fill.setAlpha(24)
                boundary=view.scene().addRect(x-rx*32,z-rz*32,(2*rx+1)*32,(2*rz+1)*32,QPen(color,2,Qt.PenStyle.DashLine),QBrush(fill))
                boundary.setToolTip(f"NPC {e['id']} movement boundary: ±{rx} X, ±{rz} Z")
            view.scene().addEllipse(x+7,z+7,18,18,QPen(color,2),QBrush(QColor('#25333d')))
            label=view.scene().addSimpleText({'npc':'N','warp':'W','background':'B'}[e['kind']]+str(e['id']))
            label.setBrush(QBrush(color));label.setPos(x+1,z+24)
            font=label.font();font.setPixelSize(9);label.setFont(font)
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        if self.ctx['area_data']['area_type']==0:
            content=project.map_scene(**self.context,image=False).get('content')
            if content:
                a,b,c,d=content['tiles'];view.setSceneRect(QRectF((a-ox)*32,(b-oz)*32,(c-a)*32,(d-b)*32))
        view.fitInView(view.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        for name in ('before_view','after_view'):
            if hasattr(self,name):
                view=getattr(self,name);view.fitInView(view.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)

    def panel(self):
        widget=QWidget();box=QVBoxLayout(widget);form=QFormLayout();box.addLayout(form)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(widget);return scroll,box,form

    def note(self,box,text):
        label=QLabel(text);label.setWordWrap(True);box.addWidget(label)

    def button(self,box,text,callback):
        b=QPushButton(text);b.clicked.connect(lambda:self.guard(callback));box.addWidget(b);return b

    def coordinates(self,form,prefix):
        ox,oz=self.ctx['origin'];x=spin(ox,ox+31,ox+16);z=spin(oz,oz+31,oz+16)
        selected=sorted(self.inspector.selected_cells)
        if selected:x.setValue(selected[0][0]+ox);z.setValue(selected[0][1]+oz)
        form.addRow('Tile X',x);form.addRow('Tile Z',z);setattr(self,prefix+'_x',x);setattr(self,prefix+'_z',z)

    def surface_panel(self):
        panel,box,form=self.panel();self.coordinates(form,'surface')
        self.surface_w=spin(1,8,1);self.surface_h=spin(1,8,1)
        form.addRow('Width',self.surface_w);form.addRow('Depth',self.surface_h)
        self.material=QComboBox();self.material.addItem('Restore original surface',None)
        try:
            for entry in self.project.surface_palette(**self.context):
                self.material.addItem(entry['texture'],entry['material'])
        except EditorError as exc:self.note(box,str(exc))
        form.addRow('Stock surface',self.material)
        self.permission=QComboBox();self.permission.addItems(['Keep movement permissions','Clear selected blocking flags','Set selected blocking flags'])
        form.addRow('Movement',self.permission)
        self.note(box,'Click the map to choose a tile. Paint exposed flat ground or floors using a compatible stock material. Raised edging, water, cliffs and baked scenery refuse. Grass appearance alone does not add encounters. Restore removes authored surface tiles.')
        self.button(box,'Add surface to preview',self.stage_surface);box.addStretch();return panel

    def stage_surface(self):
        x,z=self.surface_x.value(),self.surface_z.value();w,h=self.surface_w.value(),self.surface_h.value()
        ops=[self.op('surface',dict(x=x,z=z,width=w,height=h,material=self.material.currentData()))]
        if self.permission.currentIndex():
            flags=self.permission.currentIndex()==2;cells=[]
            for c in self.preview_project.permission_cells(**self.context,x=x,z=z,width=w,height=h)['cells']:
                before=c.get('hex') or c.get('value')
                # Get the exact pair directly from the composed permission state.
                ctx=self.ctx;offset=world.cell_offset(ctx,c['x'],c['z']);raw=self.project.member_raw(ctx['map_member'])
                pair=self.preview_project.composed()['permissions'].get((ctx['map_member'],offset),raw[offset:offset+2])
                cells.append(dict(x=c['x'],z=c['z'],before=pair.hex(),after=bytes([pair[0],pair[1]&127|(128 if flags else 0)]).hex()))
            ops.append(self.op('map',dict(permissions=cells)))
        self.stage(ops)

    def group_panel(self):
        panel,box,form=self.panel()
        self.group_action=QComboBox();
        for label,value in [('Move','move'),('Copy','duplicate'),('Delete','delete'),('Transfer to neighbor','transfer')]:self.group_action.addItem(label,value)
        form.addRow('Action',self.group_action);self.dx=spin(-32,32,0);self.dz=spin(-32,32,0)
        form.addRow('Offset X',self.dx);form.addRow('Offset Z',self.dz)
        self.destination_header=spin(0,world.header_count(self.project.blob)-1,self.context['header']);self.destination_cell=QLineEdit(','.join(map(str,self.context['cell'])))
        form.addRow('Transfer header',self.destination_header);form.addRow('Transfer cell X,Z',self.destination_cell)
        self.objects=QListWidget();self.objects.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.objects.setMinimumHeight(170);box.addWidget(self.objects)
        for p in self.inspector.scene_data['placements']:
            item=QListWidgetItem(f"{p['slot']} · {p['display']}");item.setData(Qt.ItemDataRole.UserRole,p['slot']);self.objects.addItem(item)
        self.links=QListWidget();self.links.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);self.links.setMaximumHeight(145)
        self.note(box,'Explicit event links (optional). A selected stock door includes its doorway tile and destination return link.');box.addWidget(self.links)
        for e in self.project.map_events(**self.context)['events']:
            if e['in_cell'] and e['editable_fields']:
                item=QListWidgetItem(f"{e['kind']} {e['id']} · {e['x']},{e['z']}")
                item.setData(Qt.ItemDataRole.UserRole,dict(kind=e['kind'],event_id=e['id']));self.links.addItem(item)
        self.group_collision=QCheckBox(f'Move/copy the {len(self.inspector.selected_cells)} selected collision cells');box.addWidget(self.group_collision)
        self.note(box,'Select several objects with Command/Shift. Collision comes from the cells selected in the map inspector. Copy/delete leave existing events in place. Linked event moves stay in this cell.')
        self.button(box,'Add group to preview',self.stage_group);box.addStretch();return panel

    def stage_group(self):
        action=self.group_action.currentData();destination=None
        if action=='transfer':destination=dict(header=self.destination_header.value(),cell=[int(v) for v in self.destination_cell.text().split(',')])
        ox,oz=self.ctx['origin'];cells=[dict(x=x+ox,z=z+oz) for x,z in sorted(self.inspector.selected_cells)] if self.group_collision.isChecked() else []
        ops=area_layout.group(self.preview_project,self.ctx,[i.data(Qt.ItemDataRole.UserRole) for i in self.objects.selectedItems()],
              action,self.dx.value(),self.dz.value(),destination,cells,[i.data(Qt.ItemDataRole.UserRole) for i in self.links.selectedItems()])
        self.stage(ops)

    def library_panel(self):
        panel,box,form=self.panel();self.sources=QComboBox();self.source_search=QLineEdit();self.source_search.setPlaceholderText('Filter map names')
        form.addRow('Find maps',self.source_search);form.addRow('Compatible map',self.sources)
        self.template_search=QLineEdit();self.template_search.setPlaceholderText('Find building, sign, fence…');form.addRow('Find object',self.template_search)
        self.templates=QListWidget();self.templates.setMinimumHeight(210);self.templates.setIconSize(QSize(80,64));box.addWidget(self.templates)
        self.coordinates(form,'template')
        self.note(box,'Loads stock donors from maps using compatible model and texture resources. The full donor placement is retained; target ground height is checked. Entrances and collision are separate explicit selections.')
        self.button(box,'Load compatible maps',self.load_sources);self.button(box,'Load templates',self.load_templates)
        self.button(box,'Add selected template to preview',self.stage_template)
        self.source_search.textChanged.connect(self.filter_sources);self.template_search.textChanged.connect(self.filter_templates)
        self.sources.currentIndexChanged.connect(self.source_changed)
        box.addStretch();return panel

    def source_changed(self,*_):
        self.library_template_data=[];self.loaded_source=None;self.templates.clear()

    def load_sources(self):
        self.library_source_data=area_layout.library_sources(self.project,self.ctx);self.filter_sources()

    def filter_sources(self,*_):
        self.sources.clear();query=self.source_search.text().casefold()
        for s in getattr(self,'library_source_data',[]):
            label=f"{s['name']} · {s['internal_name']} · {s['header']} / {s['cell']}"
            if query in label.casefold():self.sources.addItem(label,dict(header=s['header'],cell=s['cell']))

    def load_templates(self):
        source=self.sources.currentData()
        if source is None:raise EditorError('INVALID_INPUT','Load and choose a compatible donor map')
        self.library_template_data=area_layout.templates(self.project,self.ctx,source,images=True)
        self.loaded_source=source;self.filter_templates()

    def filter_templates(self,*_):
        self.templates.clear();query=self.template_search.text().casefold()
        for t in getattr(self,'library_template_data',[]):
            if query not in (t['display']+' '+str(t['model_id'])).casefold():continue
            item=QListWidgetItem(t['display']);item.setData(Qt.ItemDataRole.UserRole,t)
            thumb=t.get('thumbnail',{});path=thumb.get('image') if isinstance(thumb,dict) else thumb
            if path:item.setIcon(QIcon(str(path)))
            self.templates.addItem(item)

    def stage_template(self):
        item=self.templates.currentItem()
        if item is None:raise EditorError('INVALID_INPUT','Select a stock template')
        t=item.data(Qt.ItemDataRole.UserRole)
        self.stage([dict(kind='scenery',context=self.loaded_source,request=dict(operation='import',slot=t['slot'],
              x=self.template_x.value()+0.5,z=self.template_z.value()+0.5,destination=self.context))])

    def dialogue_panel(self):
        panel,box,form=self.panel();self.interaction_action=QComboBox()
        for label,value in [('New interaction','create'),('Edit authored interaction','edit'),('Duplicate authored interaction','duplicate'),('Remove authored interaction','delete')]:self.interaction_action.addItem(label,value)
        form.addRow('Action',self.interaction_action);self.authored=QComboBox();form.addRow('Authored interaction',self.authored)
        self.interaction_kind=QComboBox();self.interaction_kind.addItem('NPC','npc');self.interaction_kind.addItem('Sign interaction','background');form.addRow('Kind',self.interaction_kind)
        self.donor=QComboBox();form.addRow('Local height donor',self.donor)
        self.appearance=QComboBox();self.appearance.addItem('Keep donor / current appearance',None)
        for entry in self.project.npc_appearances():self.appearance.addItem(f"{entry['name']} · {entry['sprite']}",entry['sprite'])
        form.addRow('Stock appearance',self.appearance)
        self.coordinates(form,'dialogue');self.facing=QComboBox()
        for i,v in enumerate(('North','South','West','East')):self.facing.addItem(v,i)
        self.facing.setCurrentIndex(1);form.addRow('Facing',self.facing)
        self.movement=QComboBox()
        for value,name in npc_behavior.BEHAVIORS.items():self.movement.addItem(name,value)
        self.range_x=spin(0,8,0);self.range_z=spin(0,8,0)
        form.addRow('Behavior',self.movement);form.addRow('X range (each side)',self.range_x);form.addRow('Z range (each side)',self.range_z)
        self.movement.currentIndexChanged.connect(self.behavior_changed)
        self.dialogue=QPlainTextEdit();self.dialogue.setPlaceholderText('Plain dialogue: up to two lines, 28 characters per line.');self.dialogue.setMaximumHeight(155);box.addWidget(self.dialogue)
        self.note(box,'Choose a stock appearance and behavior. Dashed boxes show movement boundaries; blocked tiles, height changes, actors, signs, entrances and triggers must stay clear. NPCs pause to talk. Signs need an explicitly placed model/collision. Existing story actors are preserved.')
        self.button(box,'Add interaction to preview',self.stage_dialogue)
        self.interaction_kind.currentIndexChanged.connect(self.refresh_donors);self.authored.currentIndexChanged.connect(self.load_authored)
        self.interaction_action.currentIndexChanged.connect(self.interaction_action_changed)
        self.refresh_donors();self.refresh_authored();self.behavior_changed();box.addStretch();return panel

    def behavior_changed(self,*_):
        npc=self.interaction_kind.currentData()=='npc';m=self.movement.currentData()
        self.appearance.setEnabled(npc);self.movement.setEnabled(npc);self.facing.setEnabled(npc)
        for widget,allowed in [(self.range_x,m in (3,5)),(self.range_z,m in (3,4))]:
            widget.setEnabled(npc and allowed)
            if not allowed:widget.setValue(0)
            elif widget.value()==0:widget.setValue(1)

    def interaction_action_changed(self,*_):
        create=self.interaction_action.currentData()=='create'
        self.donor.setEnabled(create);self.interaction_kind.setEnabled(create)
        if not create:self.load_authored()

    def refresh_donors(self,*_):
        from . import event_authoring
        self.donor.clear()
        for e in event_authoring.records(event_authoring.base(self.project,self.ctx['event_member'])):
            if e['kind']==self.interaction_kind.currentData() and self.ctx['origin'][0]<=e['x']<self.ctx['origin'][0]+32 and self.ctx['origin'][1]<=e['z']<self.ctx['origin'][1]+32:
                self.donor.addItem(f"{e['id']} · sprite {e.get('sprite','—')} · {e['x']},{e['z']}",e['id'])
        if hasattr(self,'movement'):self.behavior_changed()

    def refresh_authored(self):
        selected=self.authored.currentData()
        self.authored.blockSignals(True);self.authored.clear()
        for s in self.preview_project.simple_interactions(**self.context):self.authored.addItem(f"{s['identity']} · {s['kind']} · {s['x']},{s['z']}",s)
        if selected:
            for i in range(self.authored.count()):
                if self.authored.itemData(i)['identity']==selected['identity']:self.authored.setCurrentIndex(i);break
        self.authored.blockSignals(False)

    def load_authored(self,*_):
        s=self.authored.currentData()
        if not s:return
        self.interaction_kind.setCurrentIndex(self.interaction_kind.findData(s['kind']))
        self.donor.setCurrentIndex(self.donor.findData(s['donor_id']))
        self.dialogue.setPlainText(s['dialogue']);self.dialogue_x.setValue(s['x']);self.dialogue_z.setValue(s['z']);self.facing.setCurrentIndex(s['facing'])
        self.appearance.setCurrentIndex(max(0,self.appearance.findData(s.get('sprite'))))
        self.movement.setCurrentIndex(self.movement.findData(s.get('movement',0)))
        self.range_x.setValue(s.get('range_x',0));self.range_z.setValue(s.get('range_z',0));self.behavior_changed()

    def stage_dialogue(self):
        action=self.interaction_action.currentData();s=self.authored.currentData()
        if action!='create' and not s:raise EditorError('INVALID_INPUT','Choose an authored interaction')
        request=dict(action=action,identity=s['identity'] if action!='create' else None,
                     kind=self.interaction_kind.currentData(),donor_id=self.donor.currentData(),
                     x=self.dialogue_x.value(),z=self.dialogue_z.value(),facing=self.facing.currentData(),dialogue=self.dialogue.toPlainText())
        if action!='delete' and self.interaction_kind.currentData()=='npc':
            request.update(sprite=self.appearance.currentData(),movement=self.movement.currentData(),range_x=self.range_x.value(),range_z=self.range_z.value())
        self.stage([self.op('interaction',request)])

    def op(self,kind,request):return dict(kind=kind,context=self.context,request=request)

    def guard(self,callback):
        try:callback()
        except (EditorError,ValueError,OSError,TypeError) as exc:self.status.setText(str(exc))

    def stage(self,ops):
        candidate=self.operations+copy.deepcopy(ops)
        plan=self.project.plan_area_edit(candidate)
        trial=self.project.area_preview_project(plan)
        scene=trial.map_scene(**self.context)
        self.operations=candidate;self.plan=plan;self.preview_project=trial
        self.queue.clear()
        for change in plan['preview']:self.queue.addItem(change['label'])
        self.show_image(self.after_view,scene['image']);self.preview_tabs.setCurrentIndex(1)
        self.apply_button.setEnabled(not plan['empty']);self.refresh_authored()
        self.status.setText(f"{len(plan['transactions'])} changes staged. Apply saves one undo step. Shared resource occurrences receive the same changes.")

    def clear(self):
        self.operations=[];self.plan=None;self.preview_project=self.project;self.queue.clear();self.apply_button.setEnabled(False)
        self.show_image(self.after_view,self.project.map_scene(**self.context)['image']);self.refresh_authored();self.status.setText('Preview cleared; nothing saved.')

    def apply(self):
        def save():
            result=self.project.apply_area_edit(self.revision,operations=self.operations)
            self.inspector.refresh();self.inspector.notice(f"Area edit saved at revision {result['revision']}; Undo restores the whole edit.")
            self.accept()
        self.guard(save)
