"""Native character library and bounded event editor, backed only by Project."""
import copy
import json
from pathlib import Path
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,QTabWidget,
    QWidget,QComboBox,QLineEdit,QPlainTextEdit,QListWidget,QSpinBox,QFileDialog,QTableWidget,QTableWidgetItem,
    QAbstractItemView,QCheckBox,QScrollArea)
from .formats import EditorError
from . import story_authoring as story, character_preview, npc_behavior, scene_commands


def spin(low,high,value=0):
    w=QSpinBox();w.setRange(low,high);w.setValue(value);return w


def pages(text): return [p.strip() for p in text.split('\n---\n') if p.strip()]
def page_text(value): return '\n---\n'.join(value)


class StoryEditor(QDialog):
    def __init__(self,inspector):
        super().__init__(inspector);self.inspector=inspector;self.project=inspector.project
        self.context={'header':inspector.header,'cell':list(inspector.cell)};self.revision=self.project.doc['revision']
        self.preview=self.project;self.operations=[];self.nodes=[];self.frames=[];self.frame=0
        self.setWindowTitle('Sovereign Editor · Characters and events');self.resize(1060,820)
        layout=QVBoxLayout(self);title=QLabel('Characters & events');title.setStyleSheet('font-size:22px;font-weight:600');layout.addWidget(title)
        note=QLabel('Import prepared art and build dialogue, battles and quest scenes. Preview stages changes; Apply saves one undoable edit.');note.setWordWrap(True);layout.addWidget(note)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        for title,builder in [('Characters',self.character_panel),('Trainers',self.trainer_panel),('Persistent state',self.state_panel),('Events',self.event_panel)]:
            content=builder();scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content);self.tabs.addTab(scroll,title)
        self.queue=QListWidget();self.queue.setMaximumHeight(85);layout.addWidget(self.queue)
        self.status=QLabel('No pending changes.');self.status.setWordWrap(True);layout.addWidget(self.status)
        row=QHBoxLayout();layout.addLayout(row);clear=QPushButton('Discard preview');clear.clicked.connect(self.clear);row.addWidget(clear);row.addStretch()
        cancel=QPushButton('Cancel');cancel.clicked.connect(self.reject);row.addWidget(cancel)
        self.apply_button=QPushButton('Apply changes');self.apply_button.setEnabled(False);self.apply_button.clicked.connect(self.apply);row.addWidget(self.apply_button)
        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.reload()

    def form(self):
        w=QWidget();layout=QVBoxLayout(w);form=QFormLayout();layout.addLayout(form);return w,layout,form

    def guard(self,fn):
        try:fn()
        except (EditorError,ValueError,OSError,KeyError) as e:self.status.setText(str(e))

    def character_panel(self):
        w,l,f=self.form();self.characters=QComboBox();f.addRow('Imported character',self.characters)
        self.characters.currentIndexChanged.connect(self.show_character)
        self.character_key=QLineEdit();self.character_key.setPlaceholderText('tiana');f.addRow('Library key',self.character_key)
        button=QPushButton('Import prepared character package…');button.clicked.connect(lambda:self.guard(self.import_character));l.addWidget(button)
        self.animation=QComboBox();self.animation.addItems(['Overworld · all 16 frames','Front','Back · throw']);self.animation.currentIndexChanged.connect(self.show_character);f.addRow('Preview',self.animation)
        self.art=QLabel('Import a character to preview its compiled frames.');self.art.setAlignment(Qt.AlignmentFlag.AlignCenter);self.art.setMinimumHeight(290);l.addWidget(self.art)
        row=QHBoxLayout();self.play=QCheckBox('Play animation');self.play.toggled.connect(self.play_changed);row.addWidget(self.play)
        self.frame_spin=spin(0,15);self.frame_spin.valueChanged.connect(self.select_frame);row.addWidget(QLabel('Frame'));row.addWidget(self.frame_spin);row.addStretch();l.addLayout(row)
        self.art_info=QLabel();self.art_info.setWordWrap(True);l.addWidget(self.art_info);l.addStretch();return w

    def import_character(self):
        path,_=QFileDialog.getOpenFileName(self,'Prepared character package','','Character package (*.json)')
        if path:self.stage('character',self.character_key.text().strip(),json.loads(Path(path).read_text()))

    def show_character(self,*_):
        if not hasattr(self,'art_info'):return
        key=self.characters.currentData();self.frames=[]
        if not key:return
        def show():
            package=self.preview.character_package(key);kind=self.animation.currentIndex()
            if kind==0:
                self.frames=[(im,160) for im in character_preview.overworld_frames(package)]
            else:
                images,anims=character_preview.trainer_frames(package['front' if kind==1 else 'back'])
                self.frames=[(images[i],max(17,round(delay*1000/60))) for i,delay in anims[-1]]
            self.frame=0;self.frame_spin.setMaximum(len(self.frames)-1);self.frame_spin.setValue(0);self.draw_frame()
            binding=self.preview.story_library()['characters'][key]
            self.art_info.setText(f"{package['name']} · overworld {binding['sprite']} · front class {binding['front_class']} · back group {binding['back_group']}\nCompiled frame preview; native acceptance remains separate.")
        self.guard(show)

    def draw_frame(self):
        if not self.frames:return
        im,delay=self.frames[self.frame];pix=QPixmap.fromImage(ImageQt(im));self.art.setPixmap(pix.scaled(im.width*3,im.height*3,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.FastTransformation))
        if self.play.isChecked():self.timer.start(delay)

    def select_frame(self,n):self.frame=n;self.draw_frame()
    def tick(self):
        if self.frames:self.frame_spin.setValue((self.frame+1)%len(self.frames))
    def play_changed(self,checked):
        if checked:self.draw_frame()
        else:self.timer.stop()

    def trainer_panel(self):
        w,l,f=self.form();self.trainers=QComboBox();self.trainers.currentIndexChanged.connect(self.load_trainer);f.addRow('Definition',self.trainers)
        self.trainer_key=QLineEdit();f.addRow('Stable key',self.trainer_key);self.trainer_name=QLineEdit();self.trainer_name.setMaxLength(10);f.addRow('Name',self.trainer_name)
        self.trainer_character=QComboBox();f.addRow('Appearance',self.trainer_character)
        self.party=QTableWidget(1,2);self.party.setHorizontalHeaderLabels(['Species number (1–493)','Level (1–100)']);self.party.horizontalHeader().setStretchLastSection(True);self.party.setColumnWidth(0,310);self.party.setMaximumHeight(160);l.addWidget(self.party)
        row=QHBoxLayout();add=QPushButton('Add Pokémon');add.clicked.connect(lambda:self.party.insertRow(self.party.rowCount()) if self.party.rowCount()<6 else None);row.addWidget(add)
        remove=QPushButton('Remove selected Pokémon');remove.clicked.connect(lambda:self.party.removeRow(self.party.currentRow()) if self.party.currentRow()>=0 else None);row.addWidget(remove);l.addLayout(row)
        self.trainer_before=QPlainTextEdit();self.trainer_after=QPlainTextEdit()
        for label,edit in [('Before battle',self.trainer_before),('After battle',self.trainer_after)]:edit.setMaximumHeight(85);edit.setPlaceholderText('Two lines per page. Separate pages with a line containing ---');f.addRow(label,edit)
        save=QPushButton('Preview trainer');save.clicked.connect(lambda:self.guard(self.stage_trainer));l.addWidget(save);l.addStretch();return w

    def load_trainer(self,*_):
        if not hasattr(self,'trainer_after'):return
        key=self.trainers.currentData();t=self.preview.story_library()['trainers'].get(key)
        self.trainer_key.setText(key or '');self.trainer_name.setText(t['name'] if t else '')
        self.party.setRowCount(len(t['party']) if t else 1)
        for i,m in enumerate(t['party'] if t else [{'species':152,'level':5}]):
            for col,k in enumerate(('species','level')):self.party.setItem(i,col,QTableWidgetItem(str(m[k])))
        self.trainer_before.setPlainText(page_text(t['before']) if t else 'Ready to practice?');self.trainer_after.setPlainText(page_text(t['after']) if t else 'Thanks for the practice!')
        if t:self.trainer_character.setCurrentIndex(self.trainer_character.findData('custom:'+t['character'] if t['character'] else 'stock:'+str(t['stock_class'])))

    def stage_trainer(self):
        appearance=self.trainer_character.currentData()
        if not appearance:raise ValueError('Import a character first.')
        appearance=appearance.split(':',1)
        party=[]
        for i in range(self.party.rowCount()):
            if not all(self.party.item(i,j) for j in range(2)):raise ValueError('Complete every party row.')
            party.append({'species':int(self.party.item(i,0).text()),'level':int(self.party.item(i,1).text())})
        self.stage('trainer',self.trainer_key.text().strip(),{'name':self.trainer_name.text(),'character':appearance[1] if appearance[0]=='custom' else None,
            'stock_class':int(appearance[1]) if appearance[0]=='stock' else None,'party':party,'before':pages(self.trainer_before.toPlainText()),'after':pages(self.trainer_after.toPlainText())})

    def state_panel(self):
        w,l,f=self.form();self.states_list=QListWidget();l.addWidget(self.states_list)
        self.state_key=QLineEdit();self.state_name=QLineEdit();f.addRow('Stable key',self.state_key);f.addRow('Display name',self.state_name)
        note=QLabel('Named values start at zero and persist in ordinary saves. Use “Set state” and “If state” steps to track progress. Existing keys retain their save slots.');note.setWordWrap(True);l.addWidget(note)
        add=QPushButton('Preview persistent state');add.clicked.connect(lambda:self.guard(lambda:self.stage('state',self.state_key.text().strip(),{'name':self.state_name.text()})));l.addWidget(add);l.addStretch();return w

    def event_panel(self):
        w,l,f=self.form();self.events=QComboBox();self.events.currentIndexChanged.connect(self.load_event);f.addRow('Definition',self.events)
        self.event_key=QLineEdit();f.addRow('Stable key',self.event_key);self.entry=QComboBox();self.entry.addItems(['NPC talk','Step on tile']);f.addRow('Trigger',self.entry)
        coords=QHBoxLayout();self.x=spin(0,65535);self.z=spin(0,65535);coords.addWidget(QLabel('X'));coords.addWidget(self.x);coords.addWidget(QLabel('Z'));coords.addWidget(self.z);f.addRow('Tile',coords)
        self.donor=spin(0,239);f.addRow('Ground-height donor NPC',self.donor);self.event_character=QComboBox();f.addRow('Character',self.event_character)
        self.facing=QComboBox();self.facing.addItems(['North','South','West','East']);f.addRow('Facing',self.facing)
        self.movement=QComboBox()
        for key,name in npc_behavior.BEHAVIORS.items():self.movement.addItem(name,key)
        f.addRow('Movement',self.movement);ranges=QHBoxLayout();self.range_x=spin(0,8);self.range_z=spin(0,8);ranges.addWidget(QLabel('X ±'));ranges.addWidget(self.range_x);ranges.addWidget(QLabel('Z ±'));ranges.addWidget(self.range_z);f.addRow('Movement range',ranges)
        self.event_form=f
        self.once=QComboBox();f.addRow('One-time state',self.once)
        self.scene_state=QComboBox();f.addRow('Quest stage state',self.scene_state)
        self.scene_values=QLineEdit();self.scene_values.setPlaceholderText('NPC: 1, 2, 3 · step-on: one value');f.addRow('Active stage values',self.scene_values)
        extent=QHBoxLayout();self.trigger_width=spin(1,9,1);self.trigger_height=spin(1,9,1)
        extent.addWidget(QLabel('Width'));extent.addWidget(self.trigger_width);extent.addWidget(QLabel('Height'));extent.addWidget(self.trigger_height);f.addRow('Step-on rectangle',extent)
        # Rows that only apply to one trigger kind; entry_fields shows the relevant ones.
        self.npc_rows=[self.event_character,self.facing,self.movement,ranges];self.trigger_rows=[extent]
        self.entry.currentIndexChanged.connect(self.entry_fields);self.entry_fields()
        self.steps=QListWidget();self.steps.setFixedHeight(110);self.steps.currentRowChanged.connect(self.load_step);l.addWidget(self.steps)
        row=QHBoxLayout();self.step_type=QComboBox();self.step_type.addItems(['say','choice','if','set','battle','move','gather','face','reaction','wait','sound','give_item','take_item','has_item','give_mon','sync','end']);row.addWidget(self.step_type)
        add=QPushButton('Add step');add.clicked.connect(self.add_step);row.addWidget(add);remove=QPushButton('Remove step');remove.clicked.connect(self.remove_step);row.addWidget(remove);l.addLayout(row)
        sf=QFormLayout();l.addLayout(sf);self.step_form=sf;self.step_id=QLineEdit();sf.addRow('Step name',self.step_id)
        self.step_pages=QPlainTextEdit();self.step_pages.setMaximumHeight(85);self.step_pages.setPlaceholderText('Two lines ×28 characters per page; separate pages with ---. Highlight with [hint]text[/hint].');sf.addRow('Dialogue pages',self.step_pages)
        self.step_state=QComboBox();self.step_value=spin(0,65535);sr=QHBoxLayout();sr.addWidget(self.step_state);sr.addWidget(self.step_value);sf.addRow('State / value',sr);self.step_state_row=sr
        self.step_trainer=QComboBox();self.step_partner=QComboBox();self.step_opponent2=QComboBox();sf.addRow('Opponent',self.step_trainer);sf.addRow('Ally (optional)',self.step_partner);sf.addRow('Second opponent',self.step_opponent2)
        self.scene_form=QFormLayout();l.addLayout(self.scene_form)
        self.step_actor=QComboBox();self.step_actor.setEditable(True)
        self.step_path=QPlainTextEdit();self.step_path.setMaximumHeight(85);self.step_path.setPlaceholderText('One X, Z waypoint per line. Horizontal or vertical segments.')
        self.step_speed=QComboBox();self.step_speed.addItems(['walk','run'])
        self.step_destination=QLineEdit();self.step_destination.setPlaceholderText('X, Z')
        self.step_direction=QComboBox();self.step_direction.addItems(['North','South','West','East'])
        self.step_reaction=QComboBox();self.step_reaction.addItems(list(scene_commands.REACTIONS))
        self.step_frames=spin(1,600,30);self.step_sound=QComboBox();self.step_sound.addItems(list(scene_commands.SOUNDS))
        self.step_item=spin(1,536,17);self.step_count=spin(1,99,1);self.step_species=spin(1,493,123);self.step_level=spin(1,100,8)
        self.scene_fields=[('Actor',self.step_actor,{'move','face','reaction'}),('Route waypoints',self.step_path,{'move'}),
            ('Speed',self.step_speed,{'move'}),('Player staging tile',self.step_destination,{'gather'}),
            ('Face direction',self.step_direction,{'face'}),('Reaction',self.step_reaction,{'reaction'}),
            ('Wait frames',self.step_frames,{'wait'}),('Sound',self.step_sound,{'sound'}),
            ('Item number',self.step_item,{'give_item','take_item','has_item'}),('Item count',self.step_count,{'give_item','take_item','has_item'}),
            ('Species number',self.step_species,{'give_mon'}),('Level',self.step_level,{'give_mon'})]
        for label,widget,_ in self.scene_fields:self.scene_form.addRow(label,widget)
        self.next_step=QLineEdit();self.no_step=QLineEdit();sf.addRow('Next / yes / won target',self.next_step);sf.addRow('No / lost target',self.no_step)
        self.complete=QCheckBox('End marks one-time event complete');self.complete.setChecked(True);sf.addRow(self.complete)
        update=QPushButton('Save selected step');update.clicked.connect(lambda:self.guard(self.save_step));l.addWidget(update)
        self.step_type.currentIndexChanged.connect(self.step_fields);self.step_fields()
        row=QHBoxLayout();save=QPushButton('Preview event');save.clicked.connect(lambda:self.guard(self.stage_event));row.addWidget(save)
        delete=QPushButton('Preview event removal');delete.clicked.connect(lambda:self.guard(lambda:self.stage('sequence',self.event_key.text().strip(),None,'delete')));row.addWidget(delete);l.addLayout(row);l.addStretch(1);return w

    def entry_fields(self,*_):
        npc=self.entry.currentIndex()==0
        for row in self.npc_rows:self.event_form.setRowVisible(row,npc)
        for row in self.trigger_rows:self.event_form.setRowVisible(row,not npc)

    def step_fields(self,*_):
        op=self.step_type.currentText()
        self.step_pages.setEnabled(op in ('say','choice'));self.step_state.setEnabled(op in ('if','set'));self.step_value.setEnabled(op in ('if','set'))
        for c in (self.step_trainer,self.step_partner,self.step_opponent2):c.setEnabled(op=='battle')
        self.next_step.setEnabled(op!='end');self.no_step.setEnabled(op in ('choice','if','battle','give_item','take_item','has_item','give_mon'));self.complete.setEnabled(op=='end')
        # Show only the rows the selected step type uses, like the scene rows below.
        for row,on in ((self.step_pages,self.step_pages.isEnabled()),(self.step_state_row,op in ('if','set')),
                       (self.step_trainer,op=='battle'),(self.step_partner,op=='battle'),(self.step_opponent2,op=='battle'),
                       (self.next_step,op!='end'),(self.no_step,self.no_step.isEnabled()),(self.complete,op=='end')):
            self.step_form.setRowVisible(row,on)
        for _,widget,ops in self.scene_fields:self.scene_form.setRowVisible(widget,op in ops)

    def add_step(self):
        n={'id':f'step{len(self.nodes)+1}','op':self.step_type.currentText()};self.nodes.append(n);self.refresh_steps(len(self.nodes)-1)
    def remove_step(self):
        i=self.steps.currentRow()
        if i>=0:self.nodes.pop(i);self.refresh_steps(min(i,len(self.nodes)-1))
    def refresh_steps(self,index=0):
        self.steps.blockSignals(True);self.steps.clear();self.steps.addItems([f"{n['id']} · {n['op']}" for n in self.nodes]);self.steps.blockSignals(False);self.steps.setCurrentRow(index);self.load_step(index)
    def load_step(self,i):
        if not hasattr(self,'complete') or not 0<=i<len(self.nodes):return
        n=self.nodes[i];self.step_id.setText(n['id']);self.step_type.setCurrentText(n['op']);self.step_pages.setPlainText(page_text(n.get('pages',[])))
        self.step_value.setValue(n.get('value',0));self.next_step.setText(n.get('next',n.get('yes',n.get('won',''))));self.no_step.setText(n.get('no',n.get('lost','')));self.complete.setChecked(n.get('complete',True))
        for widget,key in ((self.step_state,'state'),(self.step_trainer,'trainer'),(self.step_partner,'partner'),(self.step_opponent2,'opponent2')):widget.setCurrentIndex(max(0,widget.findData(n.get(key))))
        self.step_actor.setCurrentText(n.get('actor','player'));self.step_path.setPlainText('\n'.join(f'{x}, {z}' for x,z in n.get('path',[])))
        self.step_speed.setCurrentText(n.get('speed','walk'));self.step_destination.setText(', '.join(map(str,n.get('destination',[]))))
        self.step_direction.setCurrentIndex(n.get('direction',0));self.step_reaction.setCurrentText(n.get('reaction','exclamation'));self.step_sound.setCurrentText(n.get('sound','heal'))
        for widget,key,default in ((self.step_frames,'frames',30),(self.step_item,'item',17),(self.step_count,'count',1),(self.step_species,'species',123),(self.step_level,'level',8)):widget.setValue(n.get(key,default))
    def save_step(self):
        i=self.steps.currentRow()
        if i<0:raise ValueError('Add or select a step first.')
        op=self.step_type.currentText();n={'id':self.step_id.text().strip(),'op':op}
        if op in ('say','choice'):n['pages']=pages(self.step_pages.toPlainText())
        if op in ('if','set'):n.update(state=self.step_state.currentData(),value=self.step_value.value())
        if op in ('say','set'):n['next']=self.next_step.text().strip()
        if op in ('choice','if'):n.update(yes=self.next_step.text().strip(),no=self.no_step.text().strip())
        if op=='battle':n.update(trainer=self.step_trainer.currentData(),partner=self.step_partner.currentData(),opponent2=self.step_opponent2.currentData(),won=self.next_step.text().strip(),lost=self.no_step.text().strip())
        if op=='end':n['complete']=self.complete.isChecked()
        if op in scene_commands.OPS:
            if op in ('give_item','take_item','has_item','give_mon'):n.update(yes=self.next_step.text().strip(),no=self.no_step.text().strip())
            else:n['next']=self.next_step.text().strip()
            if op in ('move','face','reaction'):n['actor']=self.step_actor.currentText().strip()
            def point(text):
                values=[int(v.strip()) for v in text.split(',')]
                if len(values)!=2:raise ValueError('Enter a tile as X, Z.')
                return values
            if op=='move':n.update(path=[point(line) for line in self.step_path.toPlainText().splitlines() if line.strip()],speed=self.step_speed.currentText())
            if op=='gather':n['destination']=point(self.step_destination.text())
            if op=='face':n['direction']=self.step_direction.currentIndex()
            if op=='reaction':n['reaction']=self.step_reaction.currentText()
            if op=='wait':n['frames']=self.step_frames.value()
            if op=='sound':n['sound']=self.step_sound.currentText()
            if op in ('give_item','take_item','has_item'):n.update(item=self.step_item.value(),count=self.step_count.value())
            if op=='give_mon':n.update(species=self.step_species.value(),level=self.step_level.value())
        self.nodes[i]=n;self.refresh_steps(i)
    def load_event(self,*_):
        if not hasattr(self,'complete'):return
        key=self.events.currentData();s=self.preview.story_library()['sequences'].get(key)
        self.event_key.setText(key or '');self.entry.setCurrentIndex(0 if not s or s['kind']=='npc' else 1)
        self.nodes=copy.deepcopy(s['nodes']) if s else [{'id':'hello','op':'say','pages':['Hello!'],'next':'end'},{'id':'end','op':'end'}]
        for widget,key_,default in ((self.x,'x',0),(self.z,'z',0),(self.donor,'donor_id',0),(self.range_x,'range_x',0),(self.range_z,'range_z',0)):widget.setValue(s[key_] if s else default)
        self.facing.setCurrentIndex(s['facing'] if s else 1);self.movement.setCurrentIndex(self.movement.findData(s['movement'] if s else 0))
        appearance='#scyther' if s and s.get('stock_sprite')==552 else s['character'] if s else None
        self.event_character.setCurrentIndex(max(0,self.event_character.findData(appearance)));self.once.setCurrentIndex(max(0,self.once.findData(s['once_state'] if s else None)))
        condition=(s.get('presence') or s.get('trigger') or {}) if s else {}
        self.scene_state.setCurrentIndex(max(0,self.scene_state.findData(condition.get('state'))))
        self.scene_values.setText(', '.join(map(str,condition.get('values',[condition['value']] if 'value' in condition else []))))
        self.trigger_width.setValue(condition.get('width',1));self.trigger_height.setValue(condition.get('height',1));self.refresh_steps()
    def stage_event(self):
        self.save_step();kind='npc' if self.entry.currentIndex()==0 else 'trigger'
        value=dict(kind=kind,x=self.x.value(),z=self.z.value(),donor_id=self.donor.value(),facing=self.facing.currentIndex() if kind=='npc' else 0,movement=self.movement.currentData() if kind=='npc' else 0,
            range_x=self.range_x.value() if kind=='npc' else 0,range_z=self.range_z.value() if kind=='npc' else 0,character=self.event_character.currentData() if kind=='npc' else None,nodes=self.nodes,once_state=self.once.currentData())
        if value['character']=='#scyther':value.update(character=None,stock_sprite=552)
        if self.scene_state.currentData():
            values=[int(v.strip()) for v in self.scene_values.text().split(',')]
            if kind=='npc':value['presence']={'state':self.scene_state.currentData(),'values':values}
            else:
                if len(values)!=1:raise ValueError('A step-on scene needs one active stage value.')
                value['trigger']={'state':self.scene_state.currentData(),'value':values[0],'width':self.trigger_width.value(),'height':self.trigger_height.value()}
        self.stage('sequence',self.event_key.text().strip(),value)

    def combo(self,widget,rows,blank=None):
        old=widget.currentData();widget.blockSignals(True);widget.clear()
        if blank is not None:widget.addItem(blank,None)
        for label,key in rows:widget.addItem(label,key)
        widget.setCurrentIndex(max(0,widget.findData(old)));widget.blockSignals(False)
    def reload(self):
        lib=self.preview.story_library();characters=[(v['name'],k) for k,v in lib['characters'].items()];trainers=[(v['name'],k) for k,v in lib['trainers'].items()];states=[(v['name'],k) for k,v in lib['states'].items()]
        self.combo(self.characters,characters);self.combo(self.event_character,characters+[('Scyther (stock)','#scyther')]);self.combo(self.trainers,trainers,'New trainer')
        self.combo(self.trainer_character,[(name,'custom:'+key) for name,key in characters]+[('Stock Youngster','stock:2'),('Stock Lass','stock:3')])
        self.combo(self.once,states,'Repeatable');self.combo(self.step_state,states)
        self.combo(self.scene_state,states,'Always active')
        actor=self.step_actor.currentText();self.step_actor.clear();self.step_actor.addItems(['player']+[k for k,s in lib['sequences'].items() if s['kind']=='npc' and s['context']['header']==self.context['header']]);self.step_actor.setCurrentText(actor or 'player')
        self.combo(self.step_trainer,trainers);self.combo(self.step_partner,trainers,'No ally');self.combo(self.step_opponent2,trainers,'No second opponent')
        rows=[(key,key) for key,s in lib['sequences'].items() if s['context']['header']==self.context['header'] and s['context']['cell']==self.context['cell']]
        self.combo(self.events,rows,'New event');self.states_list.clear();self.states_list.addItems([f"{k} · {v['name']}" for k,v in lib['states'].items()]);self.show_character();self.load_trainer();self.load_event()
    def stage(self,kind,key,value,action='put'):
        operations=self.operations+[{'kind':'story','context':self.context,'request':dict(kind=kind,key=key,value=copy.deepcopy(value),action=action)}]
        plan=self.project.plan_area_edit(operations);preview=self.project.area_preview_project(plan)
        self.operations=operations;self.preview=preview;self.queue.clear();self.queue.addItems([p['label'] for p in plan['preview']]);self.apply_button.setEnabled(not plan['empty']);self.status.setText(f"Preview validated · {len(plan['transactions'])} changes. Apply to save.");self.reload()
    def clear(self):
        self.operations=[];self.preview=self.project;self.queue.clear();self.apply_button.setEnabled(False);self.status.setText('Preview discarded.');self.reload()
    def apply(self):
        def commit():
            result=self.project.apply_area_edit(self.revision,operations=self.operations,label='Characters and events');self.revision=result['revision'];self.inspector.refresh();self.accept()
        self.guard(commit)
