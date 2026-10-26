/* Read-only metadata worker. No file contents, subprocesses, sockets, or writes. */
#include <sys/types.h>
#include <sys/stat.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct { uint64_t blocks, logical, files, dirs, errors, placeholders, cloud_dirs; } Total;
typedef struct { uint64_t dev, ino; int used; } Link;
static Link *links;
static size_t linkcap = 4096, linkcount;
static Total all;
static time_t last_progress;
static const char *dataprefix="/System/Volumes/Data";

static void quoted(const char *s) {
    putchar('"');
    const unsigned char *p=(const unsigned char *)s;
    while(*p) {
        unsigned char c=*p;
        if(c=='"'||c=='\\'){putchar('\\');putchar(c);p++;continue;}
        if(c<32){printf("\\u%04x",c);p++;continue;}
        if(c<128){putchar(c);p++;continue;}
        int n=c>=0xc2&&c<=0xdf?2:c>=0xe0&&c<=0xef?3:c>=0xf0&&c<=0xf4?4:0;
        int valid=n>0;
        for(int i=1;valid&&i<n;i++)if(!p[i]||(p[i]&0xc0)!=0x80)valid=0;
        if(valid&&((c==0xe0&&p[1]<0xa0)||(c==0xed&&p[1]>=0xa0)||
                   (c==0xf0&&p[1]<0x90)||(c==0xf4&&p[1]>=0x90)))valid=0;
        if(valid){for(int i=0;i<n;i++)putchar(*p++);}
        else {printf("\\udc%02x",c);p++;}
    }
    putchar('"');
}
static int has_logical_view(const char *s) {
    const char *names[]={"/Users","/Applications","/Library","/private","/opt","/sw","/cores","/pkg",NULL};
    for(int i=0;names[i];i++) {
        size_t n=strlen(names[i]);
        if(!strncmp(s,names[i],n) && (!s[n] || s[n]=='/')) return 1;
    }
    return 0;
}
static void path_quoted(const char *s) {
    char canonical[PATH_MAX];
    if(has_logical_view(s) && snprintf(canonical,sizeof(canonical),"%s%s",dataprefix,s)<(int)sizeof(canonical))
        quoted(canonical);
    else quoted(s);
}
static uint64_t hash(uint64_t dev, uint64_t ino) {
    uint64_t x=ino ^ (dev*0x9e3779b97f4a7c15ULL);
    x=(x^(x>>30))*0xbf58476d1ce4e5b9ULL;
    return x^(x>>27);
}
static int seen_link(uint64_t dev, uint64_t ino) {
    if ((linkcount+1)*2 > linkcap) {
        size_t oldcap=linkcap; Link *old=links;
        linkcap*=2; links=calloc(linkcap,sizeof(Link));
        if (!links) { free(old); exit(70); }
        for (size_t j=0;j<oldcap;j++) if (old[j].used) {
            size_t i=hash(old[j].dev,old[j].ino)&(linkcap-1);
            while (links[i].used) i=(i+1)&(linkcap-1);
            links[i]=old[j];
        }
        free(old);
    }
    size_t i=hash(dev,ino)&(linkcap-1);
    while (links[i].used) {
        if (links[i].dev==dev && links[i].ino==ino) return 1;
        i=(i+1)&(linkcap-1);
    }
    links[i]=(Link){dev,ino,1}; linkcount++; return 0;
}
static void add(Total *to, Total from) {
    to->blocks+=from.blocks; to->logical+=from.logical;
    to->files+=from.files; to->dirs+=from.dirs;
    to->errors+=from.errors; to->placeholders+=from.placeholders;
    to->cloud_dirs+=from.cloud_dirs;
}
static void totals(Total t) {
    printf(",\"allocated_bytes\":%"PRIu64",\"logical_bytes\":%"PRIu64
           ",\"files\":%"PRIu64",\"directories\":%"PRIu64
           ",\"error_count\":%"PRIu64",\"dataless_files\":%"PRIu64
           ",\"skipped_cloud_directories\":%"PRIu64,
           t.blocks,t.logical,t.files,t.dirs,t.errors,t.placeholders,t.cloud_dirs);
}
static void error_event(const char *path, int code) {
    printf("{\"event\":\"error\",\"path\":"); path_quoted(path);
    printf(",\"errno\":%d,\"message\":",code); quoted(strerror(code)); puts("}");
}
static void expired(int sig) {
    (void)sig;
    const char msg[]="{\"event\":\"timeout\",\"complete\":false}\n";
    (void)write(STDOUT_FILENO,msg,sizeof(msg)-1); _exit(124);
}
static int useful_dir(const char *name) {
    const char *names[]={"node_modules",".git",".venv","venv","target","build",
        "dist","DerivedData","Caches","Cache","cache","logs","Logs","pkgs",NULL};
    for (int i=0;names[i];i++) if (!strcmp(name,names[i])) return 1;
    return 0;
}
static void metadata(const char *event, const char *path, const struct stat *s) {
    printf("{\"event\":"); quoted(event); printf(",\"path\":"); path_quoted(path);
    printf(",\"allocated_bytes\":%"PRIu64",\"logical_bytes\":%"PRIu64
           ",\"device\":\"%"PRIu64"\",\"inode\":\"%"PRIu64"\",\"nlink\":%u"
           ",\"mtime\":%lld,\"flags\":%u,\"dataless\":%s,\"kind\":\"%s\"}\n",
           (uint64_t)s->st_blocks*512,(uint64_t)(s->st_size>0?s->st_size:0),
           (uint64_t)s->st_dev,(uint64_t)s->st_ino,(unsigned)s->st_nlink,
           (long long)s->st_mtime,s->st_flags,(s->st_flags&SF_DATALESS)?"true":"false",
           S_ISDIR(s->st_mode)?"directory":S_ISLNK(s->st_mode)?"symlink":S_ISREG(s->st_mode)?"file":"other");
}
static void progress(void) {
    time_t current=time(NULL);
    if(current>last_progress) {printf("{\"event\":\"progress\"");totals(all);puts("}");last_progress=current;}
}
static Total leaf(const char *path,const struct stat *s) {
    Total t={.blocks=(uint64_t)s->st_blocks*512,
        .logical=(uint64_t)(S_ISREG(s->st_mode)&&s->st_size>0?s->st_size:0),
        .files=S_ISREG(s->st_mode)?1:0,
        .placeholders=(S_ISREG(s->st_mode)&&(s->st_flags&SF_DATALESS))?1:0};
    if(S_ISREG(s->st_mode) && s->st_nlink>1) {
        if(seen_link(s->st_dev,s->st_ino))t.blocks=t.logical=0;
        else metadata("hardlink",path,s);
    }
    if(S_ISREG(s->st_mode) && ((uint64_t)s->st_blocks*512>=32ULL*1024*1024 || s->st_size>=256LL*1024*1024))
        metadata("file",path,s);
    add(&all,t);
    if(t.blocks>=32ULL*1024*1024){printf("{\"event\":\"progress\"");totals(all);puts("}");last_progress=time(NULL);}
    else progress();
    return t;
}
static void dir_record(const char *path,int depth,Total t) {
    const char *name=strrchr(path,'/');name=name?name+1:path;
    if(depth<=2 || (depth<=5 && t.blocks>=1024ULL*1024) ||
       (useful_dir(name)&&t.blocks>=1024ULL*1024) || t.blocks>=256ULL*1024*1024) {
        printf("{\"event\":\"directory\",\"path\":");path_quoted(path);totals(t);puts("}");
    }
}
static Total walk_directory(const char *path,int parent,const char *name,const struct stat *s,int depth) {
    Total t={.blocks=(uint64_t)s->st_blocks*512,.dirs=1};
    add(&all,t);progress();
    if(s->st_flags&SF_DATALESS) {
        metadata("cloud_directory",path,s);t.cloud_dirs=1;all.cloud_dirs++;
        dir_record(path,depth,t);return t;
    }
    int fd=parent<0?open(path,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC):
        openat(parent,name,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(fd<0 || depth>128) {
        int code=fd<0?errno:ELOOP;if(fd>=0)close(fd);
        error_event(path,code);t.errors++;all.errors++;dir_record(path,depth,t);return t;
    }
    struct stat actual;
    if(fstat(fd,&actual) || actual.st_dev!=s->st_dev || actual.st_ino!=s->st_ino) {
        close(fd);error_event(path,ESTALE);t.errors++;all.errors++;dir_record(path,depth,t);return t;
    }
    DIR *dir=fdopendir(fd);
    if(!dir){int code=errno;close(fd);error_event(path,code);t.errors++;all.errors++;dir_record(path,depth,t);return t;}
    while(1) {
        errno=0;struct dirent *entry=readdir(dir);
        if(!entry){if(errno){error_event(path,errno);t.errors++;all.errors++;}break;}
        if(!strcmp(entry->d_name,".") || !strcmp(entry->d_name,".."))continue;
        char child[PATH_MAX];
        if(snprintf(child,sizeof(child),"%s/%s",path,entry->d_name)>=(int)sizeof(child)) {
            error_event(path,ENAMETOOLONG);t.errors++;all.errors++;continue;
        }
        struct stat info;
        /* Relative metadata queries avoid materializing online cloud packages. */
        if(fstatat(dirfd(dir),entry->d_name,&info,AT_SYMLINK_NOFOLLOW)) {
            error_event(child,errno);t.errors++;all.errors++;continue;
        }
        if(info.st_dev!=s->st_dev) {
            printf("{\"event\":\"mount\",\"path\":");path_quoted(child);puts("}");continue;
        }
        Total subtotal=S_ISDIR(info.st_mode)?walk_directory(child,dirfd(dir),entry->d_name,&info,depth+1):leaf(child,&info);
        add(&t,subtotal);
    }
    closedir(dir);dir_record(path,depth,t);return t;
}
/* Open every ancestor through directory descriptors, refusing symbolic links. */
static int open_parent(const char *path, char name[NAME_MAX+1], dev_t *device) {
    int fd=open(dataprefix,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(fd<0)return -1;
    struct stat base;
    if(fstat(fd,&base)){int code=errno;close(fd);errno=code;return -1;}
    *device=base.st_dev;
    const char *relative=path+strlen(dataprefix);
    if(*relative=='/')relative++;
    if(!*relative){strcpy(name,".");return fd;}
    char buffer[PATH_MAX];strcpy(buffer,relative);
    char *save=NULL,*part=strtok_r(buffer,"/",&save);
    while(part) {
        if(!strcmp(part,".")||!strcmp(part,"..")||strlen(part)>NAME_MAX){close(fd);errno=EINVAL;return -1;}
        char *next=strtok_r(NULL,"/",&save);
        if(!next){strcpy(name,part);return fd;}
        struct stat before,after;
        if(fstatat(fd,part,&before,AT_SYMLINK_NOFOLLOW)){int code=errno;close(fd);errno=code;return -1;}
        if(!S_ISDIR(before.st_mode)||before.st_dev!=*device||before.st_flags&SF_DATALESS) {
            close(fd);errno=S_ISLNK(before.st_mode)?ELOOP:EXDEV;return -1;
        }
        int child=openat(fd,part,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if(child<0){int code=errno;close(fd);errno=code;return -1;}
        if(fstat(child,&after)||after.st_dev!=before.st_dev||after.st_ino!=before.st_ino) {
            close(child);close(fd);errno=ESTALE;return -1;
        }
        close(fd);fd=child;part=next;
    }
    close(fd);errno=EINVAL;return -1;
}

int main(int argc,char **argv) {
    if(argc!=4||(strcmp(argv[1],"scan")&&strcmp(argv[1],"list"))) {
        fputs("Usage: space-worker scan|list /System/Volumes/Data/path seconds\n",stderr);return 64;
    }
    char *end=NULL;long seconds=strtol(argv[3],&end,10);
    if(!end||*end||seconds<1||seconds>300)return 64;
    size_t n=strlen(dataprefix);
    if(strncmp(argv[2],dataprefix,n)||(argv[2][n]&&argv[2][n]!='/')||strlen(argv[2])>=PATH_MAX)return 64;
    /* Validate traversal components before opening any selected directory. */
    char validation[PATH_MAX];strcpy(validation,argv[2]+n);
    char *saved=NULL;
    for(char *p=strtok_r(validation,"/",&saved);p;p=strtok_r(NULL,"/",&saved))
        if(!strcmp(p,".")||!strcmp(p,".."))return 64;
    setvbuf(stdout,NULL,_IOLBF,0);
    signal(SIGALRM,expired);alarm((unsigned)seconds);
    char name[NAME_MAX+1];dev_t device;
    int parent=open_parent(argv[2],name,&device);
    if(parent<0){int code=errno;error_event(argv[2],code);return code==ELOOP?64:1;}
    struct stat root;
    if(fstatat(parent,name,&root,AT_SYMLINK_NOFOLLOW)) {error_event(argv[2],errno);close(parent);return 1;}
    if(root.st_dev!=device){error_event(argv[2],EXDEV);close(parent);return 1;}
    if(!strcmp(argv[1],"list")) {
        if(!S_ISDIR(root.st_mode)) {
            metadata("entry",argv[2],&root);close(parent);puts("{\"event\":\"done\",\"complete\":true}");return 0;
        }
        if(root.st_flags&SF_DATALESS) {
            metadata("cloud_directory",argv[2],&root);close(parent);
            puts("{\"event\":\"done\",\"complete\":false,\"skipped_cloud_directories\":1}");return 1;
        }
        int fd=openat(parent,name,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        close(parent);
        if(fd<0){error_event(argv[2],errno);return 1;}
        struct stat actual;
        if(fstat(fd,&actual)||actual.st_dev!=root.st_dev||actual.st_ino!=root.st_ino){close(fd);error_event(argv[2],ESTALE);return 1;}
        DIR *dir=fdopendir(fd);
        if(!dir){int code=errno;close(fd);error_event(argv[2],code);return 1;}
        int errors=0;
        while(1) {
            errno=0;struct dirent *entry=readdir(dir);
            if(!entry){if(errno){error_event(argv[2],errno);errors++;}break;}
            if(!strcmp(entry->d_name,".")||!strcmp(entry->d_name,".."))continue;
            char child[PATH_MAX];
            if(snprintf(child,sizeof(child),"%s/%s",argv[2],entry->d_name)>=(int)sizeof(child)){
                error_event(argv[2],ENAMETOOLONG);errors++;continue;
            }
            struct stat info;
            if(fstatat(dirfd(dir),entry->d_name,&info,AT_SYMLINK_NOFOLLOW)){error_event(child,errno);errors++;}
            else metadata("entry",child,&info);
        }
        closedir(dir);printf("{\"event\":\"done\",\"complete\":%s}\n",errors?"false":"true");return errors?1:0;
    }
    links=calloc(linkcap,sizeof(Link));if(!links){close(parent);return 70;}
    if(S_ISDIR(root.st_mode))(void)walk_directory(argv[2],parent,name,&root,0);
    else (void)leaf(argv[2],&root);
    close(parent);free(links);
    printf("{\"event\":\"done\",\"complete\":%s",all.errors?"false":"true");totals(all);puts("}");
    return all.errors?1:0;
}
