import { defineComponent, h, type PropType } from 'vue';

export function nextAsinSelection(
  current: string[],
  asin: string,
  checked: boolean,
  max: number,
): string[] {
  if (!checked) return current.filter((item) => item !== asin);
  if (current.includes(asin) || current.length >= max) return current;
  return [...current, asin];
}

export default defineComponent({
  name: 'AsinBatchSelector',
  props: {
    options: { type: Array as PropType<string[]>, required: true },
    modelValue: { type: Array as PropType<string[]>, required: true },
    max: { type: Number, default: 20 },
  },
  emits: {
    'update:modelValue': (value: string[]) => Array.isArray(value),
  },
  setup(props, { emit }) {
    return () =>
      h(
        'div',
        { class: 'asin-batch-list', role: 'group', 'aria-label': '拓展流量词竞对 ASIN' },
        props.options.length
          ? props.options.map((asin) => {
              const checked = props.modelValue.includes(asin);
              return h('label', { class: 'asin-batch-option', key: asin }, [
                h('input', {
                  type: 'checkbox',
                  value: asin,
                  checked,
                  disabled: props.modelValue.length >= props.max && !checked,
                  onChange: (event: Event) => {
                    emit(
                      'update:modelValue',
                      nextAsinSelection(
                        props.modelValue,
                        asin,
                        (event.target as HTMLInputElement).checked,
                        props.max,
                      ),
                    );
                  },
                }),
                h('span', asin),
              ]);
            })
          : h('span', { class: 'asin-batch-empty' }, '暂无可选竞对 ASIN'),
      );
  },
});
