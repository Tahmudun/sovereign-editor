"""Native character library and bounded event editor, backed only by Project."""
import copy
import json
from pathlib import Path
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,QTabWidget,
    QWidget,QComboBox,QLineEdit,QPlainTextEdit,QListWidget,QSpinBox,QFileDialog,QTableWidget,QTableWidgetItem,
    QAbstractItemView,QCheckBox,QScrollArea,QHeaderView)
from .formats import EditorError
from . import story_authoring as story, character_preview, npc_behavior, scene_commands, field_moves, trainer_format


def spin(low,high,value=0):
    w=QSpinBox();w.setRange(low,high);w.setValue(value);return w


def pages(text): return [p.strip() for p in text.split('\n---\n') if p.strip()]
def tile(text):
    values=[int(v.strip()) for v in text.split(',')]
    if len(values)!=2:raise ValueError('Enter a tile as X, Z.')
    return values
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
        for title,builder in [('Characters',self.character_panel),('Trainers',self.trainer_panel),('Persistent state',self.state_panel),('Events',self.event_panel),('Field presets',self.preset_panel)]:
            content=builder();scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content);self.tabs.addTab(scroll,title)
        self.capacity_label=QLabel();self.capacity_label.setWordWrap(True);self.capacity_label.setStyleSheet('color:#8a8f98');layout.addWidget(self.capacity_label)
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
        self.trainer_policy=QComboBox();self.trainer_policy.addItem('Practice · heal before/after, local win and loss steps',None)
        self.trainer_policy.addItem('Ordinary · chosen moves/items, normal blackout on loss',story.ORDINARY)
        self.trainer_policy.addItem('Ordinary v2 · natures, IVs/EVs, abilities, AI, doubles, native sight',trainer_format.POLICY);f.addRow('Battle policy',self.trainer_policy)
        self.trainer_defeat=QComboBox();f.addRow('Defeat state (ordinary)',self.trainer_defeat)
        # Ordinary v2 (trainer_format.py): the engine's own per-trainer layout.
        self.trainer_battle=QComboBox();self.trainer_battle.addItem('Single battle','single');self.trainer_battle.addItem('Double battle (two opponents at once)','double');f.addRow('Battle type',self.trainer_battle)
        ai=QHBoxLayout();self.trainer_ai={}
        for key in trainer_format.AI_FLAGS:
            box=QCheckBox(key.replace('_',' ').capitalize());self.trainer_ai[key]=box;ai.addWidget(box)
        f.addRow('AI settings',ai);self.trainer_ai_row=ai
        self.trainer_items=QLineEdit();self.trainer_items.setPlaceholderText('Up to four item IDs the trainer may use, e.g. 17, 17');f.addRow('Trainer items',self.trainer_items)
        self.trainer_defeat_text=QPlainTextEdit();self.trainer_insufficient=QPlainTextEdit()
        for label,edit in [('In-battle defeat line',self.trainer_defeat_text),('Not enough Pokémon (double)',self.trainer_insufficient)]:edit.setMaximumHeight(60);f.addRow(label,edit)
        team=QHBoxLayout();self.team_natures=QCheckBox('Set natures');self.team_stats=QCheckBox('Set IVs and EVs');self.team_balls=QCheckBox('Set Poké Balls')
        for box in (self.team_natures,self.team_stats,self.team_balls):team.addWidget(box);box.toggled.connect(self.policy_fields)
        f.addRow('Team details',team);self.team_row=team
        self.trainer_battle.currentIndexChanged.connect(self.policy_fields)
        self.trainer_custom=QCheckBox('Choose moves for the whole team (ordinary)');l.addWidget(self.trainer_custom)
        self.party=QTableWidget(1,12);self.party.setHorizontalHeaderLabels(['Species','Level','Move 1','Move 2','Move 3','Move 4','Held item','Nature','Ability','Ball','IVs','EVs']);self.party.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);self.party.setMinimumHeight(200);l.addWidget(self.party)
        row=QHBoxLayout();add=QPushButton('Add Pokémon');add.clicked.connect(lambda:self.party_row({'species':16,'level':5,'moves':None,'held_item':0}) if self.party.rowCount()<6 else None);row.addWidget(add)
        remove=QPushButton('Remove selected Pokémon');remove.clicked.connect(lambda:self.party.removeRow(self.party.currentRow()) if self.party.currentRow()>=0 else None);row.addWidget(remove);l.addLayout(row)
        self.trainer_before=QPlainTextEdit();self.trainer_after=QPlainTextEdit();self.trainer_revisit=QPlainTextEdit()
        for label,edit in [('Before battle',self.trainer_before),('After battle',self.trainer_after),('Revisit after defeat (ordinary)',self.trainer_revisit)]:edit.setMaximumHeight(85);edit.setPlaceholderText('Two lines per page. Separate pages with a line containing ---');f.addRow(label,edit)
        self.policy_note=QLabel();self.policy_note.setWordWrap(True);l.addWidget(self.policy_note);self.trainer_page_form=f
        self.trainer_policy.currentIndexChanged.connect(self.policy_fields);self.trainer_custom.toggled.connect(self.policy_fields)
        save=QPushButton('Preview trainer');save.clicked.connect(lambda:self.guard(self.stage_trainer));l.addWidget(save);l.addStretch();return w

    def choices(self):
        if not hasattr(self,'_choices'):
            try:
                from .gameplay_ui import catalogs
                self._choices=catalogs(self.project)
            except (AttributeError,EditorError):self._choices=None   # unqualified or offline project: values only
        return self._choices

    def cell_combo(self,kind,value):
        if self.choices() is None:
            w=QComboBox();w.addItem(f'{kind} {value}',value);return w
        from .gameplay_ui import named_combo
        return named_combo(self.project,self.choices(),kind,value)

    def party_row(self,m):
        r=self.party.rowCount();self.party.insertRow(r)
        self.party.setCellWidget(r,0,self.cell_combo('species_refs' if self.choices() else 'species',m['species'] | m.get('form',0) << 11))
        self.party.setCellWidget(r,1,spin(1,100,m['level']))
        for i,move in enumerate(m.get('moves') or [0]*4):self.party.setCellWidget(r,2+i,self.cell_combo('moves',move))
        self.party.setCellWidget(r,6,self.cell_combo('items',m.get('held_item',0)))
        nature=QComboBox()
        for i,name in enumerate(trainer_format.NATURES):nature.addItem(name,i)
        nature.setCurrentIndex(m.get('nature',0));self.party.setCellWidget(r,7,nature)
        ability=QComboBox()
        for key in trainer_format.ABILITY_SLOTS:ability.addItem(key.capitalize(),key)
        ability.setCurrentIndex(max(0,ability.findData(m.get('ability','first'))));self.party.setCellWidget(r,8,ability)
        self.party.setCellWidget(r,9,spin(1,16,m.get('ball',4)))
        for c,key in ((10,'ivs'),(11,'evs')):
            edit=QLineEdit('/'.join(map(str,m.get(key,[0]*6))));edit.setToolTip('HP / Atk / Def / Spe / SpA / SpD');self.party.setCellWidget(r,c,edit)
        self.policy_fields()

    def policy_fields(self,*_):
        ordinary=self.trainer_policy.currentData() is not None;v2=self.trainer_policy.currentData()==trainer_format.POLICY
        for widget in (self.trainer_defeat,self.trainer_revisit):self.trainer_page_form.setRowVisible(widget,ordinary)
        for widget in (self.trainer_battle,self.trainer_ai_row,self.trainer_items,self.trainer_defeat_text,self.team_row):self.trainer_page_form.setRowVisible(widget,v2)
        self.trainer_page_form.setRowVisible(self.trainer_insufficient,v2 and self.trainer_battle.currentData()=='double')
        for c,on in ((7,v2 and self.team_natures.isChecked()),(8,v2),(9,v2 and self.team_balls.isChecked()),
                     (10,v2 and self.team_stats.isChecked()),(11,v2 and self.team_stats.isChecked())):self.party.setColumnHidden(c,not on)
        self.trainer_custom.setVisible(ordinary)
        for r in range(self.party.rowCount()):
            for c in range(2,7):
                cell=self.party.cellWidget(r,c)
                if cell:cell.setEnabled(ordinary and (c==6 or self.trainer_custom.isChecked()))
        self.policy_note.setText('Ordinary: stock single battle, no added healing. Winning sets the defeat state once; a revisit shows the revisit pages and never rebattles. '
                                 'Losing uses the normal blackout, heal and saved respawn, ends this event and leaves the trainer undefeated.' if ordinary else
                                 'Practice: the party is healed before and after, and win/lost steps continue this event. Moves and items use species defaults.')

    def load_trainer(self,*_):
        if not hasattr(self,'trainer_revisit'):return
        key=self.trainers.currentData();t=self.preview.story_library()['trainers'].get(key)
        self.trainer_key.setText(key or '');self.trainer_name.setText(t['name'] if t else '')
        self.trainer_policy.setCurrentIndex(max(0,self.trainer_policy.findData(t.get('policy') if t else None)))
        self.trainer_battle.setCurrentIndex(max(0,self.trainer_battle.findData((t or {}).get('battle','single'))))
        for key,box in self.trainer_ai.items():box.setChecked(key in (t or {}).get('ai',['prioritize_super_effective']))
        self.trainer_items.setText(', '.join(map(str,(t or {}).get('items',[]))))
        self.trainer_defeat_text.setPlainText(page_text((t or {}).get('defeat',[])));self.trainer_insufficient.setPlainText(page_text((t or {}).get('insufficient',[])))
        first=((t or {}).get('party') or [{}])[0]
        self.team_natures.setChecked('nature' in first);self.team_stats.setChecked('ivs' in first);self.team_balls.setChecked('ball' in first)
        self.trainer_defeat.setCurrentIndex(max(0,self.trainer_defeat.findData(t.get('defeat_state') if t else None)))
        self.party.setRowCount(0);rows=t['party'] if t else [{'species':152,'level':5}]
        self.trainer_custom.setChecked(bool(rows and rows[0].get('moves')))
        for m in rows:self.party_row(m)
        self.trainer_before.setPlainText(page_text(t['before']) if t else 'Ready to practice?');self.trainer_after.setPlainText(page_text(t['after']) if t else 'Thanks for the practice!')
        self.trainer_revisit.setPlainText(page_text(t.get('revisit',[])) if t else 'You already won this one.')
        if t:self.trainer_character.setCurrentIndex(self.trainer_character.findData('custom:'+t['character'] if t['character'] else 'stock:'+str(t['stock_class'])))
        self.policy_fields()

    def stage_trainer(self):
        appearance=self.trainer_character.currentData()
        if not appearance:raise ValueError('Import a character first.')
        appearance=appearance.split(':',1);ordinary=self.trainer_policy.currentData()
        party=[]
        for i in range(self.party.rowCount()):
            word=self.party.cellWidget(i,0).currentData();m={'species':word & 0x7ff,'level':self.party.cellWidget(i,1).value()}
            if word >> 11:m['form']=word >> 11
            if ordinary:m.update(moves=[self.party.cellWidget(i,c).currentData() for c in range(2,6)] if self.trainer_custom.isChecked() else None,
                                  held_item=self.party.cellWidget(i,6).currentData())
            party.append(m)
        value={'name':self.trainer_name.text(),'character':appearance[1] if appearance[0]=='custom' else None,
            'stock_class':int(appearance[1]) if appearance[0]=='stock' else None,'party':party,'before':pages(self.trainer_before.toPlainText()),'after':pages(self.trainer_after.toPlainText())}
        if ordinary:value.update(policy=ordinary,defeat_state=self.trainer_defeat.currentData(),revisit=pages(self.trainer_revisit.toPlainText()))
        if ordinary==trainer_format.POLICY:
            if value['defeat_state'] is None:del value['defeat_state']   # repeatable talk battle
            value.update(battle=self.trainer_battle.currentData(),ai=[k for k,b in self.trainer_ai.items() if b.isChecked()])
            items=[int(v) for v in self.trainer_items.text().replace(' ','').split(',') if v]
            if items:value['items']=items
            for key,edit in (('defeat',self.trainer_defeat_text),('insufficient',self.trainer_insufficient)):
                text=pages(edit.toPlainText())
                if text and (key=='defeat' or value['battle']=='double'):value[key]=text
            for i,m in enumerate(party):
                m['ability']=self.party.cellWidget(i,8).currentData()
                if self.team_natures.isChecked():m['nature']=self.party.cellWidget(i,7).currentData()
                if self.team_balls.isChecked():m['ball']=self.party.cellWidget(i,9).value()
                if self.team_stats.isChecked():
                    for c,key in ((10,'ivs'),(11,'evs')):
                        values=[int(v) for v in self.party.cellWidget(i,c).text().split('/')]
                        if len(values)!=6:raise ValueError('Enter six values separated by / (HP/Atk/Def/Spe/SpA/SpD).')
                        m[key]=values
        self.stage('trainer',self.trainer_key.text().strip(),value)

    def state_panel(self):
        w,l,f=self.form();self.states_list=QListWidget();l.addWidget(self.states_list)
        self.state_key=QLineEdit();self.state_name=QLineEdit();f.addRow('Stable key',self.state_key);f.addRow('Display name',self.state_name)
        self.state_switch=QCheckBox('On/off switch (0 or 1) — e.g. defeats, one-time events, visibility');f.addRow('Kind',self.state_switch)
        note=QLabel('Named values start at zero and persist in ordinary saves. Use “Set state” and “If state” steps to track progress. Existing keys retain their save slots. '
                    'Number states hold 0..65535 and can drive step-on triggers and entry stages; on/off states use their own larger pool.');note.setWordWrap(True);l.addWidget(note)
        add=QPushButton('Preview persistent state');add.clicked.connect(lambda:self.guard(self.stage_state));l.addWidget(add)
        # PROD-02: IDs may be in saves, so definitions retire (kept, unusable) instead of being deleted or reused.
        row=QHBoxLayout();self.retire_kind=QComboBox();self.retire_kind.addItems(['state','trainer','character'])
        self.retire_key=QLineEdit();self.retire_key.setPlaceholderText('key of an unused definition')
        retire=QPushButton('Preview retirement');retire.clicked.connect(lambda:self.guard(lambda:self.stage(self.retire_kind.currentText(),self.retire_key.text().strip(),None,'retire')))
        for widget in (QLabel('Retire'),self.retire_kind,self.retire_key,retire):row.addWidget(widget)
        l.addLayout(row)
        note=QLabel('Retiring keeps the ID reserved (saves may hold it) and refuses while any event, trainer or scene still uses it; new definitions always get fresh IDs.');note.setWordWrap(True);l.addWidget(note)
        l.addStretch();return w

    def stage_state(self):
        value={'name':self.state_name.text()}
        if self.state_switch.isChecked():value['switch']=True
        self.stage('state',self.state_key.text().strip(),value)

    def state_kind_changed(self,*_):
        states=self.preview.story_library()['states'];state=states.get(self.step_state.currentData()) or {}
        self.step_value.setRange(0,1 if state.get('switch') else 65535)

    def event_panel(self):
        w,l,f=self.form();self.events=QComboBox();self.events.currentIndexChanged.connect(self.load_event);f.addRow('Definition',self.events)
        self.event_key=QLineEdit();f.addRow('Stable key',self.event_key);self.entry=QComboBox();self.entry.addItems(['NPC talk','Step on tile','On entry','Sight trainer (native)']);f.addRow('Trigger',self.entry)
        coords=QHBoxLayout();self.x=spin(0,65535);self.z=spin(0,65535);coords.addWidget(QLabel('X'));coords.addWidget(self.x);coords.addWidget(QLabel('Z'));coords.addWidget(self.z);f.addRow('Tile',coords)
        self.donor=spin(0,239);f.addRow('Ground-height donor NPC',self.donor);self.event_character=QComboBox();f.addRow('Character',self.event_character)
        self.facing=QComboBox();self.facing.addItems(['North','South','West','East']);f.addRow('Facing',self.facing)
        self.movement=QComboBox()
        for key,name in npc_behavior.BEHAVIORS.items():self.movement.addItem(name,key)
        f.addRow('Movement',self.movement);ranges=QHBoxLayout();self.range_x=spin(0,8);self.range_z=spin(0,8);ranges.addWidget(QLabel('X ±'));ranges.addWidget(self.range_x);ranges.addWidget(QLabel('Z ±'));ranges.addWidget(self.range_z);f.addRow('Movement range',ranges)
        # Native sight trainers (native_trainers.py): stock trainer object, flow, messages and flag.
        self.sight_trainer=QComboBox();f.addRow('Trainer (ordinary v2)',self.sight_trainer)
        sight=QHBoxLayout();self.sight_range=spin(1,7,3);self.sight_partner=QCheckBox('Second object of a double pair')
        sight.addWidget(QLabel('Sight tiles'));sight.addWidget(self.sight_range);sight.addWidget(self.sight_partner);f.addRow('Sight',sight);self.sight_row=sight
        # Field objects (field_moves.py): the family sets the stock appearance and the
        # object owns its tile; removal returns on re-entry or is permanent via a state.
        self.field_family=QComboBox();self.field_family.addItem('Ordinary event',None)
        for key,label in (('cut','Cut tree'),('rock_smash','Rock Smash rock'),('strength','Strength boulder'),('object','Still object (gate, pickup, switch)')):self.field_family.addItem(label,key)
        f.addRow('Field object',self.field_family);self.field_look=QComboBox()
        for key in field_moves.OBJECT_LOOKS:self.field_look.addItem(key.title(),key)
        f.addRow('Object look',self.field_look);self.field_persistence=QComboBox()
        self.field_persistence.addItem('Returns when the area is re-entered','reset');self.field_persistence.addItem('Removed for good (on/off state)','permanent')
        f.addRow('After removal',self.field_persistence);self.field_state=QComboBox();f.addRow('Removal state',self.field_state)
        for widget in (self.field_family,self.field_persistence):widget.currentIndexChanged.connect(self.entry_fields)
        self.event_form=f
        self.once=QComboBox();f.addRow('One-time state',self.once)
        self.scene_state=QComboBox();f.addRow('Quest stage state',self.scene_state)
        self.scene_values=QLineEdit();self.scene_values.setPlaceholderText('NPC: 1, 2, 3 · step-on or entry: one value');f.addRow('Active stage values',self.scene_values)
        extent=QHBoxLayout();self.trigger_width=spin(1,9,1);self.trigger_height=spin(1,9,1)
        extent.addWidget(QLabel('Width'));extent.addWidget(self.trigger_width);extent.addWidget(QLabel('Height'));extent.addWidget(self.trigger_height);f.addRow('Step-on rectangle',extent)
        self.advance=spin(0,16383,1);self.advance.setToolTip('Runs once entry finishes while the stage has its active value; every exit sets this value.')
        f.addRow('Entry completes at stage',self.advance)
        # Rows that only apply to one trigger kind; entry_fields shows the relevant ones.
        self.npc_rows=[self.event_character,self.facing,self.movement,ranges];self.trigger_rows=[extent];self.entry_rows=[self.advance]
        self.native_rows=[self.sight_trainer,self.sight_row]
        self.entry.currentIndexChanged.connect(self.entry_fields);self.entry_fields()
        self.steps=QListWidget();self.steps.setFixedHeight(110);self.steps.currentRowChanged.connect(self.load_step);l.addWidget(self.steps)
        row=QHBoxLayout();self.step_type=QComboBox();self.step_type.addItems(['say','choice','if','set','battle','move','gather','face','look','pose','reaction','wait','sound','give_item','take_item','has_item','found_item','give_mon','collect','sync','tutor','relearner','give_money','shop','heal','set_spawn','require','field_move','give_badge','remove','warp','end']);row.addWidget(self.step_type)
        add=QPushButton('Add step');add.clicked.connect(self.add_step);row.addWidget(add);remove=QPushButton('Remove step');remove.clicked.connect(self.remove_step);row.addWidget(remove);l.addLayout(row)
        sf=QFormLayout();l.addLayout(sf);self.step_form=sf;self.step_id=QLineEdit();sf.addRow('Step name',self.step_id)
        self.step_pages=QPlainTextEdit();self.step_pages.setMaximumHeight(85);self.step_pages.setPlaceholderText('Two lines ×28 characters per page; separate pages with ---. Highlight with [hint]text[/hint].');sf.addRow('Dialogue pages',self.step_pages)
        self.step_state=QComboBox();self.step_state.currentIndexChanged.connect(self.state_kind_changed);self.step_value=spin(0,65535);sr=QHBoxLayout();sr.addWidget(self.step_state);sr.addWidget(self.step_value);sf.addRow('State / value',sr);self.step_state_row=sr
        self.step_trainer=QComboBox();self.step_partner=QComboBox();self.step_opponent2=QComboBox();sf.addRow('Opponent',self.step_trainer);sf.addRow('Ally (optional)',self.step_partner);sf.addRow('Second opponent',self.step_opponent2)
        self.scene_form=QFormLayout();l.addLayout(self.scene_form)
        self.step_actor=QComboBox();self.step_actor.setEditable(True)
        self.step_path=QPlainTextEdit();self.step_path.setMaximumHeight(85);self.step_path.setPlaceholderText('One X, Z waypoint per line. Horizontal or vertical segments.')
        self.step_speed=QComboBox();self.step_speed.addItems(['walk','run'])
        self.step_destination=QLineEdit();self.step_destination.setPlaceholderText('X, Z')
        self.step_target=QLineEdit();self.step_target.setPlaceholderText('Actor key or player')
        self.step_axis=QComboBox();self.step_axis.addItems(['x','z'])
        self.step_pose=QPlainTextEdit();self.step_pose.setMaximumHeight(110);self.step_pose.setPlaceholderText('{"player": {"tile": [4, 6], "facing": 2}}')
        self.step_direction=QComboBox();self.step_direction.addItems(['North','South','West','East'])
        self.step_reaction=QComboBox();self.step_reaction.addItems(list(scene_commands.REACTIONS))
        self.step_frames=spin(1,600,30);self.step_sound=QComboBox();self.step_sound.addItems(list(scene_commands.SOUNDS))
        self.step_item=spin(1,536,17);self.step_count=spin(1,99,1);self.step_species=self.cell_combo('species_refs' if self.choices() else 'species',123);self.step_level=spin(1,100,8)
        # Tutor / relearner / money services (services.py): native learn screen, payment only on success.
        self.step_move=self.cell_combo('moves',33) if self.choices() else spin(1,467,33)
        self.step_cost=QComboBox();self.step_cost.addItems(['No cost','Money','Item']);self.step_amount=spin(1,99999,500)
        self.step_eligible=QLineEdit();self.step_eligible.setPlaceholderText('Empty = any Pokémon; else keys like 545, 37:1')
        # Requirements, field actions, badges, removal and travel (field_moves.py, travel.py).
        self.step_requirement=QComboBox();self.step_requirement.addItems(['move','badge','item','state','money']);self.step_requirement.currentIndexChanged.connect(self.step_fields)
        self.step_badge=QComboBox()
        for i,name in enumerate(field_moves.BADGES):self.step_badge.addItem(f'{name} Badge ({i})',i)
        self.step_field_move=QComboBox()
        for key,spec in field_moves.FAMILIES.items():self.step_field_move.addItem(spec['name'],key)
        self.step_header=spin(0,65534,0)
        # Shops, healing and respawn (field_services.py): native special mart, stock rest heal, stock blackout spawns.
        self.step_shop=QComboBox();self.step_spawn=QComboBox()
        self.scene_fields=[('Actor',self.step_actor,{'move','face','look','reaction','collect'}),('Route waypoints',self.step_path,{'move'}),
            ('Speed',self.step_speed,{'move'}),('Requirement',self.step_requirement,{'require'}),
            ('Destination header',self.step_header,{'warp'}),('Tile (X, Z)',self.step_destination,{'gather','warp'}),
            ('Target actor / object',self.step_target,{'look','remove'}),('Diagonal priority',self.step_axis,{'look'}),
            ('Expected poses (JSON)',self.step_pose,{'pose'}),('Face / arrival direction',self.step_direction,{'face','warp'}),('Reaction',self.step_reaction,{'reaction'}),
            ('Wait frames',self.step_frames,{'wait'}),('Sound',self.step_sound,{'sound'}),
            ('Item count',self.step_count,{'give_item','take_item','has_item','found_item','tutor','relearner','require:item'}),
            ('Pokémon',self.step_species,{'give_mon'}),('Level',self.step_level,{'give_mon'}),
            ('Move',self.step_move,{'tutor','require:move'}),('Cost',self.step_cost,{'tutor','relearner'}),
            ('Money amount',self.step_amount,{'tutor','relearner','give_money','require:money'}),
            ('Item',self.step_item,{'give_item','take_item','has_item','found_item','tutor','relearner','require:item'}),
            ('Badge',self.step_badge,{'require:badge','give_badge'}),('Field move',self.step_field_move,{'field_move'}),
            ('Eligible Pokémon',self.step_eligible,{'tutor','relearner'}),
            ('Shop inventory (Records)',self.step_shop,{'shop'}),('Respawn point',self.step_spawn,{'set_spawn'})]
        for label,widget,_ in self.scene_fields:self.scene_form.addRow(label,widget)
        self.next_step=QLineEdit();self.no_step=QLineEdit();sf.addRow('Next / yes / won target',self.next_step);sf.addRow('No / lost target',self.no_step)
        self.complete=QCheckBox('End marks one-time event complete');self.complete.setChecked(True);sf.addRow(self.complete)
        update=QPushButton('Save selected step');update.clicked.connect(lambda:self.guard(self.save_step));l.addWidget(update)
        self.step_type.currentIndexChanged.connect(self.step_fields);self.step_fields()
        report=QPushButton('Review saved scene staging');report.clicked.connect(lambda:self.guard(self.review_scene));l.addWidget(report)
        row=QHBoxLayout();save=QPushButton('Preview event');save.clicked.connect(lambda:self.guard(self.stage_event));row.addWidget(save)
        delete=QPushButton('Preview event removal');delete.clicked.connect(lambda:self.guard(lambda:self.stage('sequence',self.event_key.text().strip(),None,'delete')));row.addWidget(delete);l.addLayout(row);l.addStretch(1);return w

    def preset_panel(self):
        """Field presets (presets.py): they expand into ordinary events, previewed and applied like any edit."""
        w,l,f=self.form()
        note=QLabel('Presets add ordinary events for field obstacles, gates with switches, pickups, one-time rewards, '
                    'puzzle resets and travel guides. Objects own their tile; removal returns on re-entry or is '
                    'permanent through an on/off state. Everything previews like any event and applies as one undoable edit.')
        note.setWordWrap(True);l.insertWidget(0,note)
        self.preset_kind=QComboBox()
        for key,label in (('field_obstacle','Field obstacle (Cut / Rock Smash / Strength)'),('gate','Gate with switch'),
                          ('pickup','Pickup (item on the ground)'),('reward','One-time reward giver'),
                          ('puzzle_reset','Puzzle reset helper'),('guide','Travel guide'),
                          ('rematch','Trainer with reward and rematch (one NPC)')):self.preset_kind.addItem(label,key)
        f.addRow('Preset',self.preset_kind);self.preset_key=QLineEdit();self.preset_key.setPlaceholderText('route_tree_1');f.addRow('Stable key',self.preset_key)
        coords=QHBoxLayout();self.preset_x=spin(0,65535);self.preset_z=spin(0,65535);self.preset_facing=QComboBox();self.preset_facing.addItems(['North','South','West','East']);self.preset_facing.setCurrentIndex(1)
        for label,widget in (('X',self.preset_x),('Z',self.preset_z),('Facing',self.preset_facing)):coords.addWidget(QLabel(label));coords.addWidget(widget)
        f.addRow('Tile',coords);self.preset_donor=spin(0,239);f.addRow('Ground-height donor NPC',self.preset_donor)
        self.preset_family=QComboBox()
        for key,spec in field_moves.FAMILIES.items():self.preset_family.addItem(spec['name'],key)
        self.preset_look=QComboBox()
        for key in field_moves.OBJECT_LOOKS:self.preset_look.addItem(key.title(),key)
        self.preset_persistence=QComboBox();self.preset_persistence.addItem('Returns when the area is re-entered','reset');self.preset_persistence.addItem('Removed for good (on/off state)','permanent')
        self.preset_state_mode=QComboBox();self.preset_state_mode.addItems(['New on/off state','Existing on/off state'])
        self.preset_state_key=QLineEdit();self.preset_state_key.setPlaceholderText('route_tree_1_cut');self.preset_state_name=QLineEdit();self.preset_state_name.setPlaceholderText('Route tree 1 cut')
        self.preset_state=QComboBox()
        self.preset_authority=QComboBox();self.preset_authority.addItems(['None (knowing the move is enough)','Badge','Named state','Item','Money'])
        self.preset_auth_badge=QComboBox()
        for i,name in enumerate(field_moves.BADGES):self.preset_auth_badge.addItem(f'{name} Badge ({i})',i)
        self.preset_auth_state=QComboBox();self.preset_auth_value=spin(0,65535,1);self.preset_auth_item=spin(1,1023,17);self.preset_auth_count=spin(1,99,1);self.preset_auth_money=spin(1,999999,500)
        self.preset_item=spin(1,1023,17);self.preset_count=spin(1,99,1)
        self.preset_give=QComboBox();self.preset_give.addItems(['Item','Money','Badge']);self.preset_money=spin(1,99999,500);self.preset_give_badge=QComboBox()
        for i,name in enumerate(field_moves.BADGES):self.preset_give_badge.addItem(f'{name} Badge ({i})',i)
        self.preset_appearance=QComboBox()
        self.preset_switch_key=QLineEdit();self.preset_switch_key.setPlaceholderText('route_gate_switch')
        switch=QHBoxLayout();self.preset_switch_x=spin(0,65535);self.preset_switch_z=spin(0,65535)
        for label,widget in (('X',self.preset_switch_x),('Z',self.preset_switch_z)):switch.addWidget(QLabel(label));switch.addWidget(widget)
        dest=QHBoxLayout();self.preset_dest_header=spin(0,65534,self.context['header']);self.preset_dest_x=spin(0,65535);self.preset_dest_z=spin(0,65535)
        self.preset_dest_facing=QComboBox();self.preset_dest_facing.addItems(['North','South','West','East'])
        for label,widget in (('Header',self.preset_dest_header),('X',self.preset_dest_x),('Z',self.preset_dest_z),('Facing',self.preset_dest_facing)):dest.addWidget(QLabel(label));dest.addWidget(widget)
        self.preset_original=QComboBox();self.preset_rematch=QComboBox()
        self.preset_rematch_reward=QComboBox();self.preset_rematch_reward.addItems(['No reward','Badge (once)','Item (once)'])
        self.preset_texts=QPlainTextEdit();self.preset_texts.setMaximumHeight(70);self.preset_texts.setPlaceholderText('Optional page overrides as JSON, e.g. {"blocked": "Only rangers may\\ncut here."}')
        self.preset_rows=[('Field move',self.preset_family,{'field_obstacle'}),('Look',self.preset_look,{'gate','pickup'}),
            ('After removal',self.preset_persistence,{'field_obstacle','gate','pickup'}),('State',self.preset_state_mode,{'field_obstacle','gate','pickup','reward','rematch'}),
            ('New state key',self.preset_state_key,{'field_obstacle','gate','pickup','reward','rematch'}),('New state name',self.preset_state_name,{'field_obstacle','gate','pickup','reward','rematch'}),
            ('Existing state',self.preset_state,{'field_obstacle','gate','pickup','reward','rematch'}),('Requirement',self.preset_authority,{'field_obstacle','gate','rematch'}),
            ('Required badge',self.preset_auth_badge,{'field_obstacle','gate','rematch'}),('Required state',self.preset_auth_state,{'field_obstacle','gate','rematch'}),
            ('Required state value',self.preset_auth_value,{'field_obstacle','gate','rematch'}),('Required item',self.preset_auth_item,{'field_obstacle','gate','rematch'}),
            ('Required item count',self.preset_auth_count,{'field_obstacle','gate','rematch'}),('Required money',self.preset_auth_money,{'field_obstacle','gate','rematch'}),
            ('First team (defeat state)',self.preset_original,{'rematch'}),('Rematch team (v2)',self.preset_rematch,{'rematch'}),
            ('First-win reward',self.preset_rematch_reward,{'rematch'}),
            ('Item',self.preset_item,{'pickup','reward','rematch'}),('Count',self.preset_count,{'pickup','reward','rematch'}),('Reward',self.preset_give,{'reward'}),
            ('Money',self.preset_money,{'reward'}),('Badge',self.preset_give_badge,{'reward','rematch'}),
            ('Appearance',self.preset_appearance,{'gate','reward','puzzle_reset','guide','rematch'}),('Switch key',self.preset_switch_key,{'gate'}),
            ('Switch tile',switch,{'gate'}),('Destination',dest,{'puzzle_reset','guide'}),('Text overrides',self.preset_texts,set(field_moves.FAMILIES)|{'field_obstacle','gate','pickup','reward','puzzle_reset','guide','rematch'})]
        for label,widget,_ in self.preset_rows:f.addRow(label,widget)
        self.preset_form=f
        for widget in (self.preset_kind,self.preset_persistence,self.preset_state_mode,self.preset_authority,self.preset_give,self.preset_rematch_reward):widget.currentIndexChanged.connect(self.preset_fields)
        button=QPushButton('Preview preset');button.clicked.connect(lambda:self.guard(self.stage_preset));l.addWidget(button);l.addStretch();self.preset_fields();return w

    def preset_fields(self,*_):
        kind=self.preset_kind.currentData();permanent=self.preset_persistence.currentData()=='permanent'
        prize=self.preset_rematch_reward.currentIndex() if kind=='rematch' else 0
        needs_state=kind=='reward' or kind in ('field_obstacle','gate','pickup') and permanent or prize>0
        new=self.preset_state_mode.currentIndex()==0;auth=self.preset_authority.currentIndex();give=self.preset_give.currentIndex() if kind=='reward' else -1
        extra={'State':needs_state,'New state key':needs_state and new,'New state name':needs_state and new,'Existing state':needs_state and not new,
               'Required badge':auth==1,'Required state':auth==2,'Required state value':auth==2,'Required item':auth==3,'Required item count':auth==3,
               'Required money':auth==4,'Item':kind=='pickup' or give==0 or prize==2,'Count':kind=='pickup' or give==0 or prize==2,
               'Money':give==1,'Badge':give==2 or prize==1}
        for label,widget,kinds in self.preset_rows:
            self.preset_form.setRowVisible(widget,kind in kinds and extra.get(label,True))

    def stage_preset(self):
        kind=self.preset_kind.currentData();key=self.preset_key.text().strip()
        request={'preset':kind,'key':key,'x':self.preset_x.value(),'z':self.preset_z.value(),'facing':self.preset_facing.currentIndex(),'donor_id':self.preset_donor.value()}
        texts=self.preset_texts.toPlainText().strip()
        if texts:request['texts']=json.loads(texts)
        persistence=self.preset_persistence.currentData()
        if kind in ('field_obstacle','gate','pickup'):request['persistence']='reset' if kind=='field_obstacle' and self.preset_family.currentData()=='strength' else persistence
        if kind=='reward' or kind in ('field_obstacle','gate','pickup') and request.get('persistence')=='permanent':
            request['state']=({'new':self.preset_state_key.text().strip(),'name':self.preset_state_name.text().strip()}
                              if self.preset_state_mode.currentIndex()==0 else self.preset_state.currentData())
        auth=self.preset_authority.currentIndex()
        authority=[None,{'kind':'badge','badge':self.preset_auth_badge.currentData()},
                   {'kind':'state','state':self.preset_auth_state.currentData(),'value':self.preset_auth_value.value()},
                   {'kind':'item','item':self.preset_auth_item.value(),'count':self.preset_auth_count.value()},
                   {'kind':'money','amount':self.preset_auth_money.value()}][auth]
        if kind=='field_obstacle':
            request['family']=self.preset_family.currentData()
            if authority:request['authority']=authority
        if kind in ('gate','pickup'):request['look']=self.preset_look.currentData()
        if kind=='gate':
            request['switch']={'key':self.preset_switch_key.text().strip(),'x':self.preset_switch_x.value(),'z':self.preset_switch_z.value(),
                               'facing':self.preset_facing.currentIndex(),'donor_id':self.preset_donor.value(),'appearance':self.preset_appearance.currentData()}
            if authority:request['switch']['authority']=authority
        if kind=='pickup':request.update(item=self.preset_item.value(),count=self.preset_count.value())
        if kind=='reward':
            request['appearance']=self.preset_appearance.currentData()
            request['give']=[{'item':self.preset_item.value(),'count':self.preset_count.value()},{'money':self.preset_money.value()},
                             {'badge':self.preset_give_badge.currentData()}][self.preset_give.currentIndex()]
        if kind=='rematch':
            request.update(appearance=self.preset_appearance.currentData(),original=self.preset_original.currentData(),trainer=self.preset_rematch.currentData())
            if authority:request['condition']=authority
            prize=self.preset_rematch_reward.currentIndex()
            if prize:
                request['reward']={'badge':self.preset_give_badge.currentData()} if prize==1 else {'item':self.preset_item.value(),'count':self.preset_count.value()}
                request['reward_state']=({'new':self.preset_state_key.text().strip(),'name':self.preset_state_name.text().strip()}
                                         if self.preset_state_mode.currentIndex()==0 else self.preset_state.currentData())
        if kind in ('puzzle_reset','guide'):
            request['appearance']=self.preset_appearance.currentData()
            request['destination']={'x':self.preset_dest_x.value(),'z':self.preset_dest_z.value(),'facing':self.preset_dest_facing.currentIndex()}
            if kind=='guide':request['destination']['header']=self.preset_dest_header.value()
        self.stage_operation({'kind':'preset','context':self.context,'request':request})

    def review_scene(self):
        import json
        result=self.preview.scene_report(self.event_key.text().strip())
        dialog=QDialog(self);dialog.setWindowTitle('Scene staging trace');dialog.resize(850,650)
        layout=QVBoxLayout(dialog);text=QPlainTextEdit();text.setReadOnly(True)
        text.setPlainText(json.dumps(result,indent=2));layout.addWidget(text);dialog.exec()

    def entry_fields(self,*_):
        kind=self.entry.currentIndex()
        for rows,on in ((self.npc_rows,kind in (0,3)),(self.trigger_rows,kind==1),(self.entry_rows,kind==2),(self.native_rows,kind==3)):
            for row in rows:self.event_form.setRowVisible(row,on)
        for row in (self.once,self.scene_state,self.scene_values):self.event_form.setRowVisible(row,kind!=3)
        self.event_form.setRowVisible(self.field_family,kind==0)
        family=self.field_family.currentData() if kind==0 else None
        self.event_form.setRowVisible(self.field_family,kind==0)
        self.event_form.setRowVisible(self.field_look,family=='object')
        self.event_form.setRowVisible(self.field_persistence,family not in (None,'strength'))
        self.event_form.setRowVisible(self.field_state,family not in (None,'strength') and self.field_persistence.currentData()=='permanent')
        for row in (self.event_character,self.movement):self.event_form.setRowVisible(row,kind in (0,3) and family is None)

    def step_fields(self,*_):
        op=self.step_type.currentText();keys={op}|({'require:'+self.step_requirement.currentText()} if op=='require' else set())
        state_op=op in ('if','set') or 'require:state' in keys
        self.step_pages.setEnabled(op in ('say','choice','tutor','relearner'));self.step_state.setEnabled(state_op);self.step_value.setEnabled(state_op)
        for c in (self.step_trainer,self.step_partner,self.step_opponent2):c.setEnabled(op=='battle')
        self.next_step.setEnabled(op not in ('end','warp'));self.no_step.setEnabled(op in ('choice','if','battle','give_item','take_item','has_item','give_mon','collect','gather','tutor','relearner','require','field_move'));self.complete.setEnabled(op=='end')
        # Show only the rows the selected step type uses, like the scene rows below.
        for row,on in ((self.step_pages,self.step_pages.isEnabled()),(self.step_state_row,state_op),
                       (self.step_trainer,op=='battle'),(self.step_partner,op=='battle'),(self.step_opponent2,op=='battle'),
                       (self.next_step,op not in ('end','warp')),(self.no_step,self.no_step.isEnabled()),(self.complete,op=='end')):
            self.step_form.setRowVisible(row,on)
        for _,widget,ops in self.scene_fields:self.scene_form.setRowVisible(widget,bool(keys & ops))

    def add_step(self):
        n={'id':f'step{len(self.nodes)+1}','op':self.step_type.currentText()};self.nodes.append(n);self.refresh_steps(len(self.nodes)-1)
    def remove_step(self):
        i=self.steps.currentRow()
        if i>=0:self.nodes.pop(i);self.refresh_steps(min(i,len(self.nodes)-1))
    def refresh_steps(self,index=0):
        self.steps.blockSignals(True);self.steps.clear();self.steps.addItems([f"{n['id']} · {n['op']}" for n in self.nodes]);self.steps.blockSignals(False);self.steps.setCurrentRow(index);self.load_step(index)
    def load_step(self,i):
        if not hasattr(self,'complete') or not 0<=i<len(self.nodes):return
        n=self.nodes[i];self.step_id.setText(n['id']);self.step_type.setCurrentText(n['op'])
        self.step_pages.setPlainText(page_text(n.get('pages',n.get('texts',{}).get('offer',[]))))
        cost=n.get('cost') or {};self.step_cost.setCurrentIndex(2 if 'item' in cost else 1 if 'money' in cost else 0)
        self.step_amount.setValue(cost.get('money',n.get('amount',500)))
        self.step_eligible.setText(', '.join(f"{r['species']}:{r['form']}" if r.get('form') else str(r['species']) for r in n.get('eligible',[])))
        if hasattr(self.step_move,'findData'):self.step_move.setCurrentIndex(max(0,self.step_move.findData(n.get('move',33))))
        word=n.get('species',123) | n.get('form',0) << 11;self.step_species.setCurrentIndex(max(0,self.step_species.findData(word)))
        self.step_value.setValue(n.get('value',0));self.next_step.setText(n.get('next',n.get('yes',n.get('won',''))));self.no_step.setText(n.get('no',n.get('lost','')));self.complete.setChecked(n.get('complete',True))
        for widget,key in ((self.step_state,'state'),(self.step_trainer,'trainer'),(self.step_partner,'partner'),(self.step_opponent2,'opponent2')):widget.setCurrentIndex(max(0,widget.findData(n.get(key))))
        self.step_target.setText(n.get('target',''));self.step_axis.setCurrentText(n.get('axis','x'))
        self.step_pose.setPlainText(json.dumps(n.get('actors',{}),indent=2))
        self.step_actor.setCurrentText(n.get('actor','player'));self.step_path.setPlainText('\n'.join(f'{x}, {z}' for x,z in n.get('path',[])))
        self.step_speed.setCurrentText(n.get('speed','walk'));self.step_destination.setText(', '.join(map(str,n.get('destination',[]))))
        if n['op']=='require':self.step_requirement.setCurrentText(n.get('kind','move'))
        self.step_badge.setCurrentIndex(max(0,self.step_badge.findData(n.get('badge',0))));self.step_field_move.setCurrentIndex(max(0,self.step_field_move.findData(n.get('move'))))
        self.step_header.setValue(n.get('header',self.context['header']))
        if n['op']=='warp':self.step_destination.setText(f"{n.get('x',0)}, {n.get('z',0)}")
        if n['op']=='remove':self.step_target.setText(n.get('target','self'))
        if n['op']=='shop':self.step_shop.setCurrentIndex(max(0,self.step_shop.findData(n.get('shop'))))
        if n['op']=='set_spawn':self.step_spawn.setCurrentIndex(max(0,self.step_spawn.findData('point:'+n['point'] if 'point' in n else n.get('spawn'))))
        self.step_direction.setCurrentIndex(n.get('direction',n.get('facing',0)));self.step_reaction.setCurrentText(n.get('reaction','exclamation'));self.step_sound.setCurrentText(n.get('sound','heal'))
        for widget,key,default in ((self.step_frames,'frames',30),(self.step_item,'item',17),(self.step_count,'count',1),(self.step_level,'level',8)):widget.setValue(n.get(key,default))
        if 'item' in (n.get('cost') or {}):self.step_item.setValue(n['cost']['item']);self.step_count.setValue(n['cost']['count'])
    def save_step(self):
        i=self.steps.currentRow()
        if i<0:raise ValueError('Add or select a step first.')
        op=self.step_type.currentText();n={'id':self.step_id.text().strip(),'op':op}
        if op in ('say','choice'):n['pages']=pages(self.step_pages.toPlainText())
        if op in ('if','set'):n.update(state=self.step_state.currentData(),value=self.step_value.value())
        if op in ('say','set'):n['next']=self.next_step.text().strip()
        if op in ('choice','if'):n.update(yes=self.next_step.text().strip(),no=self.no_step.text().strip())
        if op=='battle':
            n.update(trainer=self.step_trainer.currentData(),won=self.next_step.text().strip())
            # An ordinary trainer's loss blacks out; only practice battles branch locally.
            if not self.preview.story_library()['trainers'].get(n['trainer'],{}).get('policy'):
                n.update(partner=self.step_partner.currentData(),opponent2=self.step_opponent2.currentData(),lost=self.no_step.text().strip())
        if op=='end':n['complete']=self.complete.isChecked()
        if op in scene_commands.OPS:
            if op in ('give_item','take_item','has_item','give_mon','collect'):n.update(yes=self.next_step.text().strip(),no=self.no_step.text().strip())
            else:n['next']=self.next_step.text().strip()
            if op in ('move','face','look','reaction','collect'):n['actor']=self.step_actor.currentText().strip()
            def point(text):
                values=[int(v.strip()) for v in text.split(',')]
                if len(values)!=2:raise ValueError('Enter a tile as X, Z.')
                return values
            if op=='move':n.update(path=[point(line) for line in self.step_path.toPlainText().splitlines() if line.strip()],speed=self.step_speed.currentText())
            if op=='gather':
                n['destination']=point(self.step_destination.text())
                if self.no_step.text().strip():n['no']=self.no_step.text().strip()   # player on no known approach
            if op=='look':n.update(target=self.step_target.text().strip(),axis=self.step_axis.currentText())
            if op=='pose':n['actors']=json.loads(self.step_pose.toPlainText())
            if op=='face':n['direction']=self.step_direction.currentIndex()
            if op=='reaction':n['reaction']=self.step_reaction.currentText()
            if op=='wait':n['frames']=self.step_frames.value()
            if op=='sound':n['sound']=self.step_sound.currentText()
            if op in ('give_item','take_item','has_item','found_item'):n.update(item=self.step_item.value(),count=self.step_count.value())
            if op=='give_mon':
                word=self.step_species.currentData();n.update(species=word & 0x7ff,level=self.step_level.value())
                if word >> 11:n['form']=word >> 11
        if op in ('tutor','relearner'):
            n.update(yes=self.next_step.text().strip(),no=self.no_step.text().strip())
            n['texts']=dict(self.nodes[i].get('texts',{}),offer=pages(self.step_pages.toPlainText()))
            n['cost']=[None,{'money':self.step_amount.value()},{'item':self.step_item.value(),'count':self.step_count.value()}][self.step_cost.currentIndex()]
            eligible=[]
            for key in [k.strip() for k in self.step_eligible.text().split(',') if k.strip()]:
                species,_,form=key.partition(':');eligible.append({'species':int(species)} | ({'form':int(form)} if form and int(form) else {}))
            n['eligible']=eligible
            if op=='tutor':n['move']=self.step_move.currentData() if hasattr(self.step_move,'currentData') else self.step_move.value()
        if op=='give_money':n.update(amount=self.step_amount.value(),next=self.next_step.text().strip())
        if op=='require':
            kind=self.step_requirement.currentText();n.update(kind=kind,yes=self.next_step.text().strip(),no=self.no_step.text().strip())
            if kind=='move':n['move']=self.step_move.currentData() if hasattr(self.step_move,'currentData') else self.step_move.value()
            if kind=='badge':n['badge']=self.step_badge.currentData()
            if kind=='item':n.update(item=self.step_item.value(),count=self.step_count.value())
            if kind=='state':n.update(state=self.step_state.currentData(),value=self.step_value.value())
            if kind=='money':n['amount']=self.step_amount.value()
        if op=='field_move':n.update(move=self.step_field_move.currentData(),yes=self.next_step.text().strip(),no=self.no_step.text().strip())
        if op=='give_badge':n.update(badge=self.step_badge.currentData(),next=self.next_step.text().strip())
        if op=='remove':n.update(target=self.step_target.text().strip() or 'self',next=self.next_step.text().strip())
        if op=='shop':
            if self.step_shop.currentData() is None:raise ValueError('Define a shop inventory in Gameplay → Records first.')
            n.update(shop=self.step_shop.currentData(),next=self.next_step.text().strip())
        if op=='heal':n['next']=self.next_step.text().strip()
        if op=='set_spawn':
            data=self.step_spawn.currentData()
            n.update(**({'point':data[6:]} if isinstance(data,str) and data.startswith('point:') else {'spawn':data}),next=self.next_step.text().strip())
        if op=='warp':
            x,z=tile(self.step_destination.text());n.update(header=self.step_header.value(),x=x,z=z,facing=self.step_direction.currentIndex())
        self.nodes[i]=n;self.refresh_steps(i)
    def load_event(self,*_):
        if not hasattr(self,'complete'):return
        key=self.events.currentData();s=self.preview.story_library()['sequences'].get(key)
        self.event_key.setText(key or '');self.entry.setCurrentIndex({'npc':0,'trigger':1,'entry':2,'trainer':3}[s['kind']] if s else 0)
        self.sight_trainer.setCurrentIndex(max(0,self.sight_trainer.findData((s or {}).get('trainer'))));self.sight_range.setValue((s or {}).get('sight',3))
        self.sight_partner.setChecked(bool((s or {}).get('partner')))
        self.nodes=copy.deepcopy(s['nodes']) if s else [{'id':'hello','op':'say','pages':['Hello!'],'next':'end'},{'id':'end','op':'end'}]
        for widget,key_,default in ((self.x,'x',0),(self.z,'z',0),(self.donor,'donor_id',0),(self.range_x,'range_x',0),(self.range_z,'range_z',0)):widget.setValue(s[key_] if s else default)
        self.facing.setCurrentIndex(s['facing'] if s else 1);self.movement.setCurrentIndex(self.movement.findData(s['movement'] if s else 0))
        appearance=f"stock:{s['stock_sprite']}" if s and s.get('stock_sprite') is not None else s['character'] if s else None
        if appearance and appearance.startswith('stock:') and self.event_character.findData(appearance)<0:
            # History keeps an old tag's meaning; show what it is rather than a species name.
            self.event_character.addItem(f"Stock tag {appearance[6:]} (legacy · not qualified)",appearance)
        self.event_character.setCurrentIndex(max(0,self.event_character.findData(appearance)));self.once.setCurrentIndex(max(0,self.once.findData(s['once_state'] if s else None)))
        condition=(s.get('presence') or s.get('trigger') or {}) if s else {}
        self.scene_state.setCurrentIndex(max(0,self.scene_state.findData(condition.get('state'))))
        self.scene_values.setText(', '.join(map(str,condition.get('values',[condition['value']] if 'value' in condition else []))))
        self.trigger_width.setValue(condition.get('width',1));self.trigger_height.setValue(condition.get('height',1));self.advance.setValue(condition.get('advance',1))
        field=(s or {}).get('field') or {}
        self.field_family.setCurrentIndex(max(0,self.field_family.findData(field.get('family'))));self.field_look.setCurrentIndex(max(0,self.field_look.findData(field.get('look'))))
        self.field_persistence.setCurrentIndex(max(0,self.field_persistence.findData(field.get('persistence','reset'))));self.field_state.setCurrentIndex(max(0,self.field_state.findData(field.get('state'))))
        self.entry_fields();self.refresh_steps()
    def stage_event(self):
        kind=('npc','trigger','entry','trainer')[self.entry.currentIndex()]
        if kind=='trainer':
            appearance=self.event_character.currentData() or ''
            value=dict(kind=kind,x=self.x.value(),z=self.z.value(),donor_id=self.donor.value(),facing=self.facing.currentIndex(),
                       movement=self.movement.currentData(),range_x=0,range_z=0,character=None,nodes=[],once_state=None,
                       trainer=self.sight_trainer.currentData(),sight=self.sight_range.value(),partner=self.sight_partner.isChecked())
            if appearance.startswith('stock:'):value['stock_sprite']=int(appearance[6:])
            self.stage('sequence',self.event_key.text().strip(),value);return
        self.save_step()
        value=dict(kind=kind,x=self.x.value(),z=self.z.value(),donor_id=self.donor.value(),facing=self.facing.currentIndex() if kind=='npc' else 0,movement=self.movement.currentData() if kind=='npc' else 0,
            range_x=self.range_x.value() if kind=='npc' else 0,range_z=self.range_z.value() if kind=='npc' else 0,character=self.event_character.currentData() if kind=='npc' else None,nodes=self.nodes,once_state=self.once.currentData())
        if (value['character'] or '').startswith('stock:'):value.update(character=None,stock_sprite=int(value['character'][6:]))
        family=self.field_family.currentData() if kind=='npc' else None
        if family:
            value.pop('stock_sprite',None);value.update(character=None,movement=0,range_x=0,range_z=0)
            persistence='reset' if family=='strength' else self.field_persistence.currentData()
            value['field']={'family':family,'persistence':persistence}
            if family=='object':value['field']['look']=self.field_look.currentData()
            if persistence=='permanent':value['field']['state']=self.field_state.currentData()
        if self.scene_state.currentData():
            values=[int(v.strip()) for v in self.scene_values.text().split(',')]
            if kind=='npc':value['presence']={'state':self.scene_state.currentData(),'values':values}
            else:
                if len(values)!=1:raise ValueError('A step-on or entry scene needs one active stage value.')
                value['trigger']={'state':self.scene_state.currentData(),'value':values[0]}
                value['trigger'].update({'width':self.trigger_width.value(),'height':self.trigger_height.value()} if kind=='trigger' else {'advance':self.advance.value()})
        self.stage('sequence',self.event_key.text().strip(),value)

    def combo(self,widget,rows,blank=None):
        old=widget.currentData();widget.blockSignals(True);widget.clear()
        if blank is not None:widget.addItem(blank,None)
        for label,key in rows:widget.addItem(label,key)
        widget.setCurrentIndex(max(0,widget.findData(old)));widget.blockSignals(False)
    def reload(self):
        lib=self.preview.story_library();characters=[(v['name'],k) for k,v in lib['characters'].items() if not v.get('retired')];trainers=[(v['name'],k) for k,v in lib['trainers'].items() if not v.get('retired')];states=[(v['name'],k) for k,v in lib['states'].items() if not v.get('retired')]
        stock=[(f"{v['name']} (stock {v['stock_sprite']})",f"stock:{v['stock_sprite']}") for v in lib['stock_sprites']]
        self.combo(self.characters,characters);self.combo(self.event_character,characters+stock);self.combo(self.trainers,trainers,'New trainer')
        self.combo(self.trainer_character,[(name,'custom:'+key) for name,key in characters]+[('Stock Youngster','stock:2'),('Stock Lass','stock:3')])
        self.combo(self.once,states,'Repeatable');self.combo(self.step_state,states);self.combo(self.trainer_defeat,states,'Repeatable (v2 talk battle)')
        self.combo(self.scene_state,states,'Always active')
        switches=[(v['name'],k) for k,v in lib['states'].items() if v.get('switch') and not v.get('retired')]
        self.combo(self.field_state,switches);self.combo(self.preset_state,switches);self.combo(self.preset_auth_state,states)
        self.combo(self.preset_appearance,[(label,int(key[6:])) for label,key in stock])
        actor=self.step_actor.currentText();self.step_actor.clear();self.step_actor.addItems(['player']+[k for k,s in lib['sequences'].items() if s['kind']=='npc' and s['context']['header']==self.context['header']]);self.step_actor.setCurrentText(actor or 'player')
        self.combo(self.step_trainer,trainers);self.combo(self.step_partner,trainers,'No ally');self.combo(self.step_opponent2,trainers,'No second opponent')
        self.combo(self.step_shop,[(f"{r['name']} · {len(r['items'])} items (mart {r['id']})",r['name']) for r in self.preview.data_catalog('shops',limit=600)['entries']])
        try:travel=[(f"{r['name']} · authored respawn (spawn {r['spawn']})",'point:'+r['key']) for r in self.preview.travel_view()['points'] if r['blackout'] and not r['retired']]
        except EditorError:travel=[]
        self.combo(self.step_spawn,[(f"{r['name']} · {r['map']} ({r['x']}, {r['z']})",r['id']) for r in self.preview.data_catalog('spawns',limit=40)['entries'] if r['supported']]+travel)
        v2=[(v['name']+f' ({k})',k) for k,v in lib['trainers'].items() if v.get('policy')==trainer_format.POLICY and not v.get('retired')]
        self.combo(self.sight_trainer,v2);self.combo(self.preset_original,trainers);self.combo(self.preset_rematch,v2)
        rows=[(key,key) for key,s in lib['sequences'].items() if s['context']['header']==self.context['header'] and s['context']['cell']==self.context['cell']]
        self.combo(self.events,rows,'New event');self.states_list.clear()
        self.states_list.addItems([f"{k} · {v['name']} · "+(f"on/off (flag {v['flag']:#x})" if v.get('switch') else f"number (variable {v['variable']:#x})")+(' · retired' if v.get('retired') else '') for k,v in lib['states'].items()])
        self.show_capacity();self.state_kind_changed();self.show_character();self.load_trainer();self.load_event()
    def show_capacity(self):
        c=self.preview.capacity()['limits']
        self.capacity_label.setText(f"Capacity · states {c['named_states']['used']}/{c['named_states']['limit']} "
            f"(number {c['number_states']['used']}/{c['number_states']['limit']}, on/off {c['switch_states']['used']}/{c['switch_states']['limit']}) · "
            f"characters {c['characters']['used']}/{c['characters']['limit']} · trainers {c['trainers']['used']}/{c['trainers']['limit']} · "
            f"scene visibility {c['visibility_actors']['used']}/{c['visibility_actors']['limit']}")
    def stage(self,kind,key,value,action='put'):
        self.stage_operation({'kind':'story','context':self.context,'request':dict(kind=kind,key=key,value=copy.deepcopy(value),action=action)},kind,key,action)
    def stage_operation(self,operation,kind=None,key=None,action='put'):
        operations=self.operations+[operation]
        plan=self.project.plan_area_edit(operations);preview=self.project.area_preview_project(plan)
        self.operations=operations;self.preview=preview;self.queue.clear();self.queue.addItems([p['label'] for p in plan['preview']]);self.apply_button.setEnabled(not plan['empty']);self.status.setText(f"Preview validated · {len(plan['transactions'])} changes. Apply to save.");self.reload()
        if kind=='trainer' and action=='put':self.trainers.setCurrentIndex(max(0,self.trainers.findData(key)))   # keep the staged definition in view
    def clear(self):
        self.operations=[];self.preview=self.project;self.queue.clear();self.apply_button.setEnabled(False);self.status.setText('Preview discarded.');self.reload()
    def apply(self):
        def commit():
            result=self.project.apply_area_edit(self.revision,operations=self.operations,label='Characters and events');self.revision=result['revision'];self.inspector.refresh();self.accept()
        self.guard(commit)
