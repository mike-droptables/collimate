import {render,screen} from '@testing-library/react';
import {Star, CodeXml, Sparkles, WandSparkles, PaintbrushVertical, Funnel, FileBraces, ChartColumn, ChartLine, ChartPie} from 'lucide-react';
import {describe,expect,it} from 'vitest';
import {DynamicIcon} from './DynamicIcon';

describe('DynamicIcon',()=>{
    it('keeps stored group keys rendering their existing SVG geometry __F1861',()=>{
        const cases=[['code2',CodeXml],['stars',Sparkles],['wand2',WandSparkles],['paintbrush2',PaintbrushVertical],
            ['filter',Funnel],['file-json',FileBraces],['bar-chart',ChartColumn],['line-chart',ChartLine],['pie-chart',ChartPie]] as const;
        const view=render(<>{cases.map(([name,Icon])=><Icon key={name} aria-label={name}/>)}</>);
        const shapes=cases.map(([name])=>screen.getByLabelText(name).innerHTML);
        view.rerender(<>{cases.map(([name])=><DynamicIcon key={name} name={name} aria-label={name}/>)}</>);
        cases.forEach(([name],index)=>{
            const glyph=screen.getByLabelText(name);
            expect(glyph).toHaveAttribute('viewBox','0 0 24 24');
            expect(glyph.innerHTML).toBe(shapes[index]);
            expect(glyph.querySelector('path,circle,rect,line,polyline')).not.toBeNull();
        });
    });
    it('renders the selected registry glyph and forwards accessible presentation __F0955',()=>{
        render(<DynamicIcon name="zap" className="named-glyph" aria-label="Selected icon" aria-hidden={false}/>);
        const glyph=screen.getByLabelText('Selected icon');
        expect(glyph).toHaveClass('lucide-zap','named-glyph');
        expect(glyph).toHaveAttribute('aria-hidden','false');
    });

    it('keeps a usable default or explicit fallback when no registered glyph exists __F0955',()=>{
        const view=render(<DynamicIcon aria-label="Default icon"/>);
        expect(screen.getByLabelText('Default icon')).toHaveClass('lucide-boxes');
        view.rerender(<DynamicIcon name="not-a-registered-icon" fallback={Star} aria-label="Fallback icon"/>);
        expect(screen.getByLabelText('Fallback icon')).toHaveClass('lucide-star');
        view.rerender(<DynamicIcon name={null} fallback={Star} aria-label="Null icon"/>);
        expect(screen.getByLabelText('Null icon')).toHaveClass('lucide-star');
    });
});
