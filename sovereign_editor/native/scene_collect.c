/* Targeted field collection, pinned HG runtime e97c7fc / Sovereign baseline.
 * Reuses the native ball resource, initialization and draw callback. Unlike
 * ov01_02203890, every sprite/visibility operation uses the selected object.
 * No follower globals. This is an effect, not a capture probability mechanic.
 * Build: tools/build_scene_collect.py. No runtime compiler dependency.
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef int BOOL;
#define FN(addr, ret, ...) ((ret (*)(__VA_ARGS__))((addr) | 1))
#define ReadHalf FN(0x0203fe2c,u16,void*)
#define FindObject FN(0x0205ee60,void*,void*,u32)
#define SpriteId FN(0x0205f25c,u32,void*)
#define Sprite FN(0x021f72dc,void*,void*)
#define Scale FN(0x02023e78,void,void*,const int*)
#define Valid FN(0x0205f0f8,BOOL,void*,u32,u32,u32)
#define Active FN(0x021fc004,void,void*,BOOL)
#define Hide FN(0x02069dc8,void,void*,BOOL)
#define Destroy FN(0x021f1640,void,void*)
#define Payload FN(0x02068d98,u32*,void*)
#define InitBall FN(0x02203820,BOOL,void*,u32*)
#define Manager FN(0x021f146c,void*,void*)
#define Field FN(0x021f1468,void*,void*)
#define ResourceSlot FN(0x021f1588,void*,void*,u32)
#define Position FN(0x0205f944,void,void*,int*)
#define Priority FN(0x0205f09c,u32,void*,u32)
#define Spawn FN(0x021f1620,void*,void*,const u32*,const int*,u32,const void*,u32)
#define VarPointer FN(0x02040374,u16*,void*,u16)
#define Native FN(0x0203fd58,void,void*,void*)

/* ScriptContext.data at 0x64, field at 0x80. Effect scratch retains the stock
 * 0x54 bytes plus a pointer to the waiting context and its success status. */
int collect_init(void *effect, u32 *w) {
    u32 *payload = Payload(effect);
    w[21] = payload[9];
    w[22] = 0;
    w[4] = 0;
    return InitBall(effect,w);
}
void collect_destroy(void *effect, u32 *w) {
    (void)effect;
    void *obj = (void*)w[12];
    if (Valid(obj,w[1],w[2],w[3])) {
        void *sprite = Sprite(obj);
        if (sprite) { int scale[3]={4096,4096,4096}; Scale(sprite,scale); }
    }
    Active((u8*)w[11]+0x3c,0);
    volatile u32 *data=(u32*)w[21];
    data[1]=w[22];
    data[0]=1;
}
void collect_update(void *effect, u32 *w) {
    void *obj=(void*)w[12];
    if (!Valid(obj,w[1],w[2],w[3])) { Destroy(effect); return; }
    if (w[0]==0) {
        void *sprite=Sprite(obj);
        if (!sprite) { Destroy(effect); return; }
        u32 frame=++w[4];
        int n=4096-(int)frame*341;
        int scale[3]={n,n,4096};
        Scale(sprite,scale);
        if (frame==12) {
            Hide(obj,1);
            Active((u8*)w[11]+0x3c,1);
            w[0]=1; w[4]=0; w[22]=1;
        }
    } else if (++w[4]>=24) { Destroy(effect); }
}
const u32 collect_descriptor[5]={0x5c,(u32)collect_init,(u32)collect_destroy,
                               (u32)collect_update,0x022037e9};
int collect_wait(u32 *ctx) {
    volatile u32 *data=ctx+25;
    if (!data[0]) return 0;
    *VarPointer((void*)ctx[32],0x800a)=(u16)data[1];
    return 1;
}
int collect_command(u32 *ctx) {
    u16 id=ReadHalf(ctx);
    void *field=(void*)ctx[32];
    *VarPointer(field,0x800a)=0;
    /* Player/follower IDs must never be accepted, including raw bytecode. */
    if (id>=253) return 0;
    void *obj=FindObject(*(void**)((u8*)field+0x3c),id);
    if (!obj) return 0;
    u32 sprite_id=SpriteId(obj);
    if (sprite_id<364 || sprite_id>993 || !Sprite(obj)) return 0;
    void *manager=Manager(obj);
    /* The stock resource getter asserts on an absent renderer; inspect the
     * optional slot first so an unsupported field can take the failure branch. */
    void *slot=ResourceSlot(manager,0x11);
    if (!slot) return 0;
    void *resource=*(void**)((u8*)slot+4);
    if (!resource) return 0;
    u32 payload[10];
    Position(obj,(int*)payload);
    payload[2]+=6*4096;
    payload[3]=(u32)Field(manager);payload[4]=(u32)manager;
    payload[5]=(u32)resource;payload[6]=(u32)obj;
    payload[7]=0;payload[8]=255;payload[9]=(u32)(ctx+25);
    int position[3];Position(obj,position);
    ctx[25]=0;ctx[26]=0;
    void *effect=Spawn(manager,collect_descriptor,position,1,payload,Priority(obj,2));
    if (!effect) return 0;
    Native(ctx,collect_wait);
    return 1;
}
