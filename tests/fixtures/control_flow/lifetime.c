/* Synthetic CC0 control-flow fixture; not the complete cJSON implementation. */
typedef struct Item { int type; struct Item *child; char *valuestring; } Item;
enum { cJSON_IsReference = 256 };
void release(void *p);
void cJSON_Delete(Item *item) {
    if (!(item->type & cJSON_IsReference) && item->child != 0) {
        cJSON_Delete(item->child);
    }
    if (!(item->type & cJSON_IsReference) && item->valuestring != 0) {
        release(item->valuestring);
    }
    release(item);
}
int choices(int x) {
    if (x < 0) return 1; /* nonzero is not automatically an error */
    if (x > 0) { if (x > 9) x = 9; } else { x = 2; }
    switch (x) {
        case 1: x += 1; /* deliberate fallthrough */
        case 2: x += 2; break;
        default: x = 0;
    }
    for (int i = 0; i < 3; ++i) x += i;
    while (x > 20) --x;
    do { ++x; } while (x < 2);
    return x;
}
